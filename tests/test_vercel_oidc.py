"""Vercel OIDC settings, token checks and the cache of Vercel's signing keys.

Every key is generated when the tests run, and a stand-in answers the key set
requests, so no test reaches Vercel.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, wait
from types import ModuleType
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from telemetry_collector.app import CollectorSettings
from telemetry_collector.intake import INTAKE_SWITCH
from telemetry_collector.session_tokens import SIGNING_KEY_VARIABLE
from tests.vercel_oidc_fakes import (
    AUDIENCE,
    DASHBOARD_PROJECT_ID,
    ISSUER,
    KEY_SET_URL,
    OTHER_PROJECT_ID,
    SETTINGS_ENVIRONMENT,
    TEAM_ID,
    TEAM_SLUG,
    FakeClock,
    FakeKeySetAddress,
    ec_public_jwk,
    hmac_signed_token,
    key_set_answer,
    rsa_private_key,
    rsa_public_jwk,
    signed_token,
    token_claims,
    unsigned_token,
)

VERCEL_OIDC_MODULE = "telemetry_collector.vercel_oidc"
TEAM_SLUG_SETTING = "TELEMETRY_VERCEL_OIDC_TEAM_SLUG"
TEAM_ID_SETTING = "TELEMETRY_VERCEL_OIDC_TEAM_ID"
READERS_SETTING = "TELEMETRY_VERCEL_OIDC_READERS"

SIGNING_KEY = rsa_private_key()
SIGNING_KEY_ID = "signing-key-1"
SIGNING_JWK = rsa_public_jwk(SIGNING_KEY, SIGNING_KEY_ID)
# The listed public key as PEM text, which an attacker can use as an HS256
# secret, hoping the verifier passes it to HMAC.
SIGNING_PUBLIC_KEY_PEM = SIGNING_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
# The key Vercel signs with after rotating its keys.
ROTATED_KEY = rsa_private_key()
ROTATED_KEY_ID = "signing-key-2"
ROTATED_JWK = rsa_public_jwk(ROTATED_KEY, ROTATED_KEY_ID)
# A key Vercel never lists.
UNLISTED_KEY = rsa_private_key()
# Listed with a `use` or `alg` the collector must not verify RS256 tokens with.
ENCRYPTION_KEY = rsa_private_key()
RS512_KEY = rsa_private_key()

ONE_MINUTE = 60
FRESH_SECONDS = 10 * 60
STALE_KEY_LIFETIME_SECONDS = 24 * 60 * 60
# How long a test waits for another thread, so that a regression fails the
# test rather than holding it forever.
THREAD_WAIT_SECONDS = 5
# How long a held key set fetch waits for the test to release it: clearly
# longer than any wait of the test's, so that a request that waits for the
# fetch is still waiting when the test checks it.
HELD_FETCH_SECONDS = 3 * THREAD_WAIT_SECONDS


def in_background(function: Callable[..., Any], *arguments: Any) -> Future:
    """Runs ``function`` in a daemon thread. Unlike a thread pool's, the
    thread is never joined, so a regression that never returns fails the test
    on its bounded waits without holding pytest at exit."""

    future: Future = Future()

    def run() -> None:
        try:
            future.set_result(function(*arguments))
        except BaseException as error:  # noqa: BLE001 - handed to the test
            future.set_exception(error)

    threading.Thread(target=run, daemon=True).start()
    return future


def vercel_oidc() -> ModuleType:
    assert importlib.util.find_spec(VERCEL_OIDC_MODULE) is not None
    return importlib.import_module(VERCEL_OIDC_MODULE)


def configured_settings() -> Any:
    settings = vercel_oidc().VercelOidcSettings.from_environment(SETTINGS_ENVIRONMENT)
    assert settings is not None
    return settings


def verifier_serving(
    answer: httpx.Response | Exception,
) -> tuple[Any, FakeKeySetAddress, FakeClock]:
    """A verifier whose key set requests ``answer`` answers, with its clock."""

    address = FakeKeySetAddress(answer)
    clock = FakeClock()
    verifier = vercel_oidc().VercelOidcVerifier(
        configured_settings(), http_get=address.get, clock=clock
    )
    return verifier, address, clock


def dashboard_token(**claim_changes: Any) -> str:
    return signed_token(SIGNING_KEY, SIGNING_KEY_ID, token_claims(**claim_changes))


# Settings


@pytest.mark.parametrize("unset_value", [None, "", "  \n"])
def test_vercel_oidc_reads_are_off_without_settings(unset_value: str | None) -> None:
    environment = (
        {}
        if unset_value is None
        else {name: unset_value for name in SETTINGS_ENVIRONMENT}
    )

    assert vercel_oidc().VercelOidcSettings.from_environment(environment) is None


def test_all_three_settings_give_the_team_and_its_readers() -> None:
    module = vercel_oidc()

    settings = module.VercelOidcSettings.from_environment(
        {
            **SETTINGS_ENVIRONMENT,
            READERS_SETTING: (
                "  prj_dashboard:preview\nprj_dashboard:production\t"
                "prj_other:development prj_dashboard:preview  "
            ),
        }
    )

    assert settings == module.VercelOidcSettings(
        team_slug=TEAM_SLUG,
        team_id=TEAM_ID,
        readers=frozenset(
            {
                module.VercelOidcReader(DASHBOARD_PROJECT_ID, "preview"),
                module.VercelOidcReader(DASHBOARD_PROJECT_ID, "production"),
                module.VercelOidcReader(OTHER_PROJECT_ID, "development"),
            }
        ),
    )
    assert settings.issuer == ISSUER
    assert settings.audience == AUDIENCE


@pytest.mark.parametrize(
    "set_names",
    [
        (TEAM_SLUG_SETTING,),
        (TEAM_ID_SETTING,),
        (READERS_SETTING,),
        (TEAM_SLUG_SETTING, TEAM_ID_SETTING),
        (TEAM_SLUG_SETTING, READERS_SETTING),
        (TEAM_ID_SETTING, READERS_SETTING),
    ],
)
def test_part_of_the_settings_stops_startup_naming_the_missing_ones(
    set_names: tuple[str, ...],
) -> None:
    module = vercel_oidc()
    environment = {name: SETTINGS_ENVIRONMENT[name] for name in set_names}

    with pytest.raises(RuntimeError) as raised:
        module.VercelOidcSettings.from_environment(environment)

    message = str(raised.value)
    for name in SETTINGS_ENVIRONMENT:
        if name not in set_names:
            assert name in message
    for value in environment.values():
        assert value not in message


@pytest.mark.parametrize(
    ("name", "malformed_value"),
    [
        (TEAM_SLUG_SETTING, "Example-Team"),
        (TEAM_SLUG_SETTING, "-example-team"),
        (TEAM_SLUG_SETTING, "example-team-"),
        (TEAM_SLUG_SETTING, "example_team"),
        (TEAM_SLUG_SETTING, "example-team/other"),
        (TEAM_SLUG_SETTING, "e" * 101),
        (TEAM_ID_SETTING, "example"),
        (TEAM_ID_SETTING, "team_"),
        (TEAM_ID_SETTING, "team_example-1"),
        (TEAM_ID_SETTING, "team_" + "e" * 65),
        (READERS_SETTING, "prj_dashboard"),
        (READERS_SETTING, "prj_dashboard:staging"),
        (READERS_SETTING, "prj_dashboard:Preview"),
        (READERS_SETTING, "prj_:preview"),
        (READERS_SETTING, "prj_dashboard:preview,prj_dashboard:production"),
        (READERS_SETTING, "prj_dashboard:preview team_example:production"),
    ],
)
def test_a_malformed_setting_stops_startup_naming_it_but_not_its_value(
    name: str, malformed_value: str
) -> None:
    module = vercel_oidc()

    with pytest.raises(RuntimeError) as raised:
        module.VercelOidcSettings.from_environment(
            {**SETTINGS_ENVIRONMENT, name: malformed_value}
        )

    assert name in str(raised.value)
    assert malformed_value not in str(raised.value)


def test_the_collector_reads_its_vercel_oidc_settings_when_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = vercel_oidc()
    for name in [*SETTINGS_ENVIRONMENT, SIGNING_KEY_VARIABLE, INTAKE_SWITCH]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEMETRY_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("TELEMETRY_READ_TOKEN", "y" * 24)

    assert CollectorSettings.from_environment().vercel_oidc is None

    for name, value in SETTINGS_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    assert CollectorSettings.from_environment().vercel_oidc == (
        module.VercelOidcSettings.from_environment(SETTINGS_ENVIRONMENT)
    )


# Token checks


@pytest.mark.parametrize("environment", ["preview", "production"])
def test_a_valid_token_gives_its_vercel_project_and_environment(
    environment: str,
) -> None:
    module = vercel_oidc()
    verifier, _, _ = verifier_serving(key_set_answer(SIGNING_JWK))

    reader = verifier.verify(dashboard_token(environment=environment))

    assert reader == module.VercelOidcReader(DASHBOARD_PROJECT_ID, environment)


def _seconds_from_now(seconds: int) -> int:
    return int(time.time()) + seconds


# Each token the collector refuses as invalid, by what is wrong with it. The
# tokens are made when a test runs, so that their times are current.
INVALID_TOKENS: dict[str, Callable[[], str]] = {
    "another team's issuer": lambda: dashboard_token(
        iss="https://oidc.vercel.com/other-team"
    ),
    "the global issuer": lambda: dashboard_token(iss="https://oidc.vercel.com"),
    "another team's audience": lambda: dashboard_token(
        aud="https://vercel.com/other-team"
    ),
    "expired": lambda: dashboard_token(exp=_seconds_from_now(-120)),
    "not yet valid": lambda: dashboard_token(nbf=_seconds_from_now(300)),
    "issued in the future": lambda: dashboard_token(iat=_seconds_from_now(300)),
    "without exp": lambda: dashboard_token(without=("exp",)),
    "without iat": lambda: dashboard_token(without=("iat",)),
    "without sub": lambda: dashboard_token(without=("sub",)),
    "without iss": lambda: dashboard_token(without=("iss",)),
    "without aud": lambda: dashboard_token(without=("aud",)),
    "without owner_id": lambda: dashboard_token(without=("owner_id",)),
    "without project_id": lambda: dashboard_token(without=("project_id",)),
    "a project_id that is not a string": lambda: dashboard_token(project_id=7),
    "an empty environment": lambda: dashboard_token(environment=""),
    "HS256": lambda: jwt.encode(
        token_claims(), "s" * 32, algorithm="HS256", headers={"kid": SIGNING_KEY_ID}
    ),
    "HS256 with the listed RSA public key's PEM as its secret": lambda: (
        hmac_signed_token(SIGNING_PUBLIC_KEY_PEM, SIGNING_KEY_ID)
    ),
    "RS512": lambda: jwt.encode(
        token_claims(), SIGNING_KEY, algorithm="RS512", headers={"kid": SIGNING_KEY_ID}
    ),
    "none": lambda: unsigned_token(
        json.dumps({"alg": "none", "typ": "JWT", "kid": SIGNING_KEY_ID})
    ),
    "without kid": lambda: jwt.encode(token_claims(), SIGNING_KEY, algorithm="RS256"),
    "an empty kid": lambda: jwt.encode(
        token_claims(), SIGNING_KEY, algorithm="RS256", headers={"kid": ""}
    ),
    "an unknown kid": lambda: signed_token(UNLISTED_KEY, "unlisted-key"),
    "another key's signature": lambda: signed_token(UNLISTED_KEY, SIGNING_KEY_ID),
    "longer than 8,192 characters": lambda: dashboard_token(padding="p" * 8192),
    # Refused by the length check before any parsing; the RecursionError a
    # header nested too deeply raises is tested by
    # test_an_error_while_parsing_a_token_refuses_it_as_invalid.
    "a deeply nested header, refused for its length": lambda: unsigned_token(
        '{"alg":"RS256","kid":"signing-key-1","nested":'
        + "[" * 100_000
        + "]" * 100_000
        + "}"
    ),
    "not a JWT": lambda: "not-a-jwt",
    "three parts that are not a JWT": lambda: "not.a.jwt",
    "empty": lambda: "",
}


@pytest.mark.parametrize("case", sorted(INVALID_TOKENS))
def test_an_invalid_token_is_refused_as_invalid(case: str) -> None:
    module = vercel_oidc()
    verifier, _, _ = verifier_serving(key_set_answer(SIGNING_JWK))

    with pytest.raises(module.InvalidVercelOidcTokenError):
        verifier.verify(INVALID_TOKENS[case]())


def test_a_token_of_up_to_8192_characters_is_checked() -> None:
    verifier, _, _ = verifier_serving(key_set_answer(SIGNING_JWK))
    unpadded_length = len(dashboard_token(padding=""))
    # Base64url gives four characters for every three bytes of claims, so not
    # every length can be made: take the longest of a few near the limit.
    padding_length = (8192 - unpadded_length) * 3 // 4
    candidates = [
        dashboard_token(padding="p" * (padding_length + extra))
        for extra in range(-4, 5)
    ]
    longest = max((token for token in candidates if len(token) <= 8192), key=len)

    assert len(longest) >= 8190
    assert verifier.verify(longest).project_id == DASHBOARD_PROJECT_ID


@pytest.mark.parametrize(
    "claim_changes",
    [
        {"owner_id": "team_other"},
        {"project_id": OTHER_PROJECT_ID},
        {"environment": "development"},
    ],
    ids=["another team", "another project", "another environment"],
)
def test_a_valid_token_of_a_reader_not_configured_may_not_read(
    claim_changes: dict[str, str],
) -> None:
    module = vercel_oidc()
    verifier, _, _ = verifier_serving(key_set_answer(SIGNING_JWK))

    with pytest.raises(module.VercelOidcReaderNotAllowedError):
        verifier.verify(dashboard_token(**claim_changes))


@pytest.mark.parametrize("error_type", [RecursionError, ValueError, TypeError])
@pytest.mark.parametrize("parsing_function", ["get_unverified_header", "decode"])
def test_an_error_while_parsing_a_token_refuses_it_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
    parsing_function: str,
    error_type: type[Exception],
) -> None:
    # A header or claims nested too deeply for Python's JSON decoder raise
    # RecursionError, which would otherwise answer 500.
    module = vercel_oidc()
    verifier, _, _ = verifier_serving(key_set_answer(SIGNING_JWK))
    token = dashboard_token()

    def refuse(*arguments: Any, **options: Any) -> Any:
        raise error_type("The token cannot be parsed.")

    monkeypatch.setattr(jwt, parsing_function, refuse)

    with pytest.raises(module.InvalidVercelOidcTokenError):
        verifier.verify(token)


def test_no_token_reaches_a_log_or_an_error_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    module = vercel_oidc()
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    accepted = dashboard_token()
    refused = [
        dashboard_token(exp=_seconds_from_now(-120)),
        dashboard_token(project_id=OTHER_PROJECT_ID),
        signed_token(UNLISTED_KEY, SIGNING_KEY_ID),
    ]

    verifier.verify(accepted)
    for token in refused:
        with pytest.raises(module.VercelOidcError) as raised:
            verifier.verify(token)
        assert token not in str(raised.value)
    address.answer = httpx.ConnectError("Vercel cannot be reached.")
    clock.advance(STALE_KEY_LIFETIME_SECONDS + 1)
    with pytest.raises(module.VercelOidcKeysUnavailableError) as raised:
        verifier.verify(accepted)
    assert accepted not in str(raised.value)

    for token in [accepted, *refused]:
        assert token not in caplog.text
        # Nor its claims or signature.
        for part in token.split("."):
            assert part not in caplog.text


# Vercel's signing keys


def test_the_key_set_is_fetched_only_from_vercel_s_address_without_redirects() -> None:
    module = vercel_oidc()
    verifier, address, _ = verifier_serving(key_set_answer(SIGNING_JWK))

    verifier.verify(dashboard_token())

    # Vercel's own verifier (@vercel/oidc's verifyVercelOidcToken) reads every
    # issuer's keys there, team issuers included.
    assert (
        module.KEY_SET_URL
        == KEY_SET_URL
        == ("https://oidc.vercel.com/.well-known/jwks")
    )
    assert address.requests == [
        (KEY_SET_URL, {"timeout": 5, "follow_redirects": False})
    ]


def test_the_verifier_fetches_with_httpx_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = vercel_oidc()
    address = FakeKeySetAddress(key_set_answer(SIGNING_JWK))
    monkeypatch.setattr(httpx, "get", address.get)
    verifier = module.VercelOidcVerifier(configured_settings(), clock=FakeClock())

    verifier.verify(dashboard_token())

    assert address.requests == [
        (KEY_SET_URL, {"timeout": 5, "follow_redirects": False})
    ]


def test_a_known_key_is_used_without_fetching_while_the_set_is_fresh() -> None:
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    token = dashboard_token()

    verifier.verify(token)
    clock.advance(FRESH_SECONDS - 1)
    verifier.verify(token)

    assert len(address.requests) == 1

    clock.advance(2)
    verifier.verify(token)

    assert len(address.requests) == 2


def test_an_unknown_key_id_fetches_the_set_again_at_most_once_a_minute() -> None:
    module = vercel_oidc()
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    verifier.verify(dashboard_token())
    # Vercel starts signing with a new key.
    address.answer = key_set_answer(SIGNING_JWK, ROTATED_JWK)
    rotated_token = signed_token(ROTATED_KEY, ROTATED_KEY_ID)

    clock.advance(30)
    with pytest.raises(module.InvalidVercelOidcTokenError):
        verifier.verify(rotated_token)
    assert len(address.requests) == 1

    clock.advance(31)
    assert verifier.verify(rotated_token).project_id == DASHBOARD_PROJECT_ID
    assert len(address.requests) == 2

    # Tokens naming a key Vercel doesn't list fetch the set once a minute at
    # most, however many arrive.
    unlisted_token = signed_token(UNLISTED_KEY, "unlisted-key")
    for _attempt in range(3):
        clock.advance(25)
        with pytest.raises(module.InvalidVercelOidcTokenError):
            verifier.verify(unlisted_token)
    assert len(address.requests) == 3


def test_after_a_failed_fetch_listed_keys_stay_usable_for_24_hours() -> None:
    module = vercel_oidc()
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    token = dashboard_token()
    verifier.verify(token)
    address.answer = httpx.Response(503)

    clock.advance(FRESH_SECONDS + 1)
    verifier.verify(token)
    clock.advance(30)
    verifier.verify(token)

    assert len(address.requests) == 2

    # One second before 24 hours from the last successful fetch.
    clock.advance(STALE_KEY_LIFETIME_SECONDS - FRESH_SECONDS - 32)
    verifier.verify(token)

    assert len(address.requests) == 3

    clock.advance(2)
    with pytest.raises(module.VercelOidcKeysUnavailableError):
        verifier.verify(token)


def test_an_unknown_key_id_while_vercel_cannot_be_reached_is_unavailable() -> None:
    module = vercel_oidc()
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    verifier.verify(dashboard_token())
    address.answer = httpx.ConnectError("Vercel cannot be reached.")

    clock.advance(ONE_MINUTE + 1)
    with pytest.raises(module.VercelOidcKeysUnavailableError):
        verifier.verify(signed_token(ROTATED_KEY, ROTATED_KEY_ID))
    # Its listed key still works.
    assert verifier.verify(dashboard_token()).project_id == DASHBOARD_PROJECT_ID


def test_without_a_successful_fetch_every_token_is_unavailable() -> None:
    module = vercel_oidc()
    verifier, _, _ = verifier_serving(httpx.ConnectTimeout("Vercel did not answer."))

    with pytest.raises(module.VercelOidcKeysUnavailableError):
        verifier.verify(dashboard_token())


def test_once_a_fetch_succeeds_without_a_key_id_its_tokens_are_invalid() -> None:
    module = vercel_oidc()
    verifier, address, clock = verifier_serving(
        httpx.ConnectError("Vercel cannot be reached.")
    )
    with pytest.raises(module.VercelOidcKeysUnavailableError):
        verifier.verify(dashboard_token())

    clock.advance(ONE_MINUTE + 1)
    address.answer = key_set_answer(SIGNING_JWK)

    with pytest.raises(module.InvalidVercelOidcTokenError):
        verifier.verify(signed_token(ROTATED_KEY, ROTATED_KEY_ID))
    assert verifier.verify(dashboard_token()).project_id == DASHBOARD_PROJECT_ID
    assert len(address.requests) == 2


# Key sets that list keys, none of which the collector can verify RS256 tokens
# with: a successful fetch needs at least one usable key.
KEY_SETS_WITHOUT_A_USABLE_KEY = {
    "an empty key set": lambda: key_set_answer(),
    "a key set without a usable key": lambda: key_set_answer(
        ec_public_jwk("ec-key"),
        rsa_public_jwk(ENCRYPTION_KEY, "encryption-key", use="enc"),
        {"kty": "RSA", "kid": "broken-key", "e": "AQAB"},
    ),
}


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, json={"keys": [SIGNING_JWK]}),
        httpx.Response(404),
        httpx.Response(
            302, headers={"Location": "https://keys.example/.well-known/jwks"}
        ),
        httpx.Response(200, text="<html>Not a key set</html>"),
        httpx.Response(200, json=[SIGNING_JWK]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"keys": {SIGNING_KEY_ID: SIGNING_JWK}}),
        httpx.Response(200, content=b"[" * 100_000 + b"]" * 100_000),
        httpx.ConnectError("Vercel cannot be reached."),
        httpx.ReadTimeout("Vercel did not answer in time."),
        *(answer() for answer in KEY_SETS_WITHOUT_A_USABLE_KEY.values()),
    ],
    ids=[
        "a server error",
        "not found",
        "a redirect",
        "not JSON",
        "a list",
        "no keys",
        "keys that are not a list",
        "JSON nested too deeply to parse",
        "no connection",
        "a timeout",
        *KEY_SETS_WITHOUT_A_USABLE_KEY,
    ],
)
def test_an_answer_without_a_usable_key_is_a_failed_fetch(
    answer: httpx.Response | Exception,
) -> None:
    module = vercel_oidc()
    verifier, _, _ = verifier_serving(answer)

    with pytest.raises(module.VercelOidcKeysUnavailableError):
        verifier.verify(dashboard_token())


@pytest.mark.parametrize("answer_name", sorted(KEY_SETS_WITHOUT_A_USABLE_KEY))
def test_a_key_set_without_a_usable_key_keeps_the_cached_keys(
    answer_name: str,
) -> None:
    module = vercel_oidc()
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    token = dashboard_token()
    rotated_token = signed_token(ROTATED_KEY, ROTATED_KEY_ID)
    verifier.verify(token)
    address.answer = KEY_SETS_WITHOUT_A_USABLE_KEY[answer_name]()

    # The cached key, no longer fresh, still verifies after such an answer.
    clock.advance(FRESH_SECONDS + 1)
    assert verifier.verify(token).project_id == DASHBOARD_PROJECT_ID
    assert len(address.requests) == 2

    # A key the cache doesn't hold is unavailable, not invalid, while the last
    # fetch failed.
    clock.advance(ONE_MINUTE)
    with pytest.raises(module.VercelOidcKeysUnavailableError):
        verifier.verify(rotated_token)
    assert len(address.requests) == 3

    # The cached key stays usable for 24 hours after the last successful
    # fetch, and no longer.
    clock.advance(STALE_KEY_LIFETIME_SECONDS - FRESH_SECONDS - 1 - ONE_MINUTE)
    assert verifier.verify(token).project_id == DASHBOARD_PROJECT_ID
    assert len(address.requests) == 4
    clock.advance(1)
    with pytest.raises(module.VercelOidcKeysUnavailableError):
        verifier.verify(token)


def test_keys_the_collector_cannot_verify_rs256_tokens_with_are_skipped() -> None:
    module = vercel_oidc()
    verifier, address, _ = verifier_serving(
        key_set_answer(
            ec_public_jwk("ec-key"),
            rsa_public_jwk(ENCRYPTION_KEY, "encryption-key", use="enc"),
            rsa_public_jwk(RS512_KEY, "rs512-key", alg="RS512"),
            rsa_public_jwk(UNLISTED_KEY, None),
            {**rsa_public_jwk(UNLISTED_KEY, None), "kid": 7},
            {"kty": "RSA", "kid": "broken-key", "e": "AQAB"},
            "not a key",
            SIGNING_JWK,
        )
    )

    assert verifier.verify(dashboard_token()).project_id == DASHBOARD_PROJECT_ID
    for private_key, key_id in [
        (ENCRYPTION_KEY, "encryption-key"),
        (RS512_KEY, "rs512-key"),
        (UNLISTED_KEY, "ec-key"),
        (UNLISTED_KEY, "broken-key"),
    ]:
        with pytest.raises(module.InvalidVercelOidcTokenError):
            verifier.verify(signed_token(private_key, key_id))
    assert len(address.requests) == 1


def test_a_listed_key_without_use_or_alg_is_used() -> None:
    verifier, _, _ = verifier_serving(
        key_set_answer(rsa_public_jwk(SIGNING_KEY, SIGNING_KEY_ID, use=None, alg=None))
    )

    assert verifier.verify(dashboard_token()).project_id == DASHBOARD_PROJECT_ID


def test_concurrent_requests_with_no_fresh_keys_share_one_fetch() -> None:
    verifier, address, _ = verifier_serving(key_set_answer(SIGNING_JWK))
    token = dashboard_token()
    # Long enough that every request arrives while the first fetch is waiting.
    address.before_answering = lambda: time.sleep(0.2)

    requests = [in_background(verifier.verify, token) for _ in range(8)]
    readers = [request.result(timeout=THREAD_WAIT_SECONDS) for request in requests]

    assert len(address.requests) == 1
    assert {reader.project_id for reader in readers} == {DASHBOARD_PROJECT_ID}


class HeldFetch:
    """Holds each key set fetch until the test releases it, after noting that
    one started. It waits for the release at most ``HELD_FETCH_SECONDS``."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.released = threading.Event()

    def __call__(self) -> None:
        self.started.set()
        self.released.wait(timeout=HELD_FETCH_SECONDS)


def test_a_fresh_cached_key_never_waits_for_another_thread_s_fetch() -> None:
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    cached_token = dashboard_token()
    verifier.verify(cached_token)
    address.answer = key_set_answer(SIGNING_JWK, ROTATED_JWK)
    held_fetch = HeldFetch()
    address.before_answering = held_fetch
    clock.advance(ONE_MINUTE + 1)

    # A token whose key the cache doesn't hold makes its thread fetch.
    rotated = in_background(verifier.verify, signed_token(ROTATED_KEY, ROTATED_KEY_ID))
    try:
        assert held_fetch.started.wait(timeout=THREAD_WAIT_SECONDS)
        cached = in_background(verifier.verify, cached_token)
        finished, _ = wait([cached], timeout=THREAD_WAIT_SECONDS)
        # It returned while the other thread's fetch was still held.
        assert cached in finished
    finally:
        held_fetch.released.set()
    assert cached.result().project_id == DASHBOARD_PROJECT_ID
    reader = rotated.result(timeout=THREAD_WAIT_SECONDS)

    assert reader.project_id == DASHBOARD_PROJECT_ID
    assert len(address.requests) == 2


def test_a_stale_cached_key_is_used_while_another_thread_fetches_again() -> None:
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    cached_token = dashboard_token()
    verifier.verify(cached_token)
    address.answer = key_set_answer(SIGNING_JWK, ROTATED_JWK)
    held_fetch = HeldFetch()
    address.before_answering = held_fetch
    clock.advance(FRESH_SECONDS + 1)

    # The first request to find the cached keys no longer fresh fetches.
    refreshing = in_background(verifier.verify, cached_token)
    try:
        assert held_fetch.started.wait(timeout=THREAD_WAIT_SECONDS)
        # Meanwhile a request with the same key uses it, still stale ...
        stale = in_background(verifier.verify, cached_token)
        # ... and one whose key the cache doesn't hold waits for the fetch.
        rotated = in_background(
            verifier.verify, signed_token(ROTATED_KEY, ROTATED_KEY_ID)
        )
        finished, _ = wait([stale], timeout=THREAD_WAIT_SECONDS)
        assert stale in finished
    finally:
        held_fetch.released.set()
    assert stale.result().project_id == DASHBOARD_PROJECT_ID
    refreshed_reader = refreshing.result(timeout=THREAD_WAIT_SECONDS)
    rotated_reader = rotated.result(timeout=THREAD_WAIT_SECONDS)

    assert refreshed_reader.project_id == DASHBOARD_PROJECT_ID
    # The rotated key came from the first request's fetch: only one thread
    # fetched.
    assert rotated_reader.project_id == DASHBOARD_PROJECT_ID
    assert len(address.requests) == 2


def test_a_stale_key_is_used_when_another_thread_holds_the_fetch_lock() -> None:
    # A fetch is due, but another thread holds the fetch lock (here, the
    # test), so the request uses its stale key instead of waiting for it.
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    cached_token = dashboard_token()
    verifier.verify(cached_token)
    clock.advance(FRESH_SECONDS + 1)

    assert verifier._fetch_lock.acquire(timeout=THREAD_WAIT_SECONDS)
    try:
        stale = in_background(verifier.verify, cached_token)
        finished, _ = wait([stale], timeout=THREAD_WAIT_SECONDS)
        assert stale in finished
    finally:
        verifier._fetch_lock.release()
    assert stale.result().project_id == DASHBOARD_PROJECT_ID
    assert len(address.requests) == 1


def test_an_unknown_key_waits_for_a_fetch_in_progress_only_so_long() -> None:
    # A burst of tokens naming keys nobody lists must not hold every worker
    # thread for as long as a slow fetch takes: past the wait, the answer is
    # that the keys are unavailable.
    module = vercel_oidc()
    verifier, address, clock = verifier_serving(key_set_answer(SIGNING_JWK))
    verifier._fetch_wait_seconds = 0.2
    clock.advance(ONE_MINUTE + 1)

    assert verifier._fetch_lock.acquire(timeout=THREAD_WAIT_SECONDS)
    try:
        unknown = in_background(
            verifier.verify, signed_token(UNLISTED_KEY, "unlisted-key")
        )
        finished, _ = wait([unknown], timeout=THREAD_WAIT_SECONDS)
        assert unknown in finished
    finally:
        verifier._fetch_lock.release()
    with pytest.raises(module.VercelOidcKeysUnavailableError):
        unknown.result()
    assert address.requests == []


def test_an_unknown_key_waits_for_a_fetch_up_to_ten_seconds() -> None:
    verifier, _, _ = verifier_serving(key_set_answer(SIGNING_JWK))

    assert getattr(verifier, "_fetch_wait_seconds", None) == 10
