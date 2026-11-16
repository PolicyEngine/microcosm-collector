"""Read API v2 answers exactly as its OpenAPI document and recorded interactions say.

Version 1, which the dashboard reads today, keeps its recorded answers too.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import Security
from fastapi.security import HTTPBearer
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from telemetry_collector.app import create_app
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.read_api_v2 import run_detail
from telemetry_collector.storage import PostgresTelemetryStore
from tests.fakes import FakeTelemetryStore
from tests.read_api_contract import (
    INTERACTIONS_DIR,
    OPENAPI_PATH,
    READ_HEADERS,
    V1_INTERACTIONS_DIR,
    ContractAuthenticator,
    answer,
    contract_client,
    contract_settings,
    load_json,
    mismatched_interactions,
    recorded_interactions,
    store_contract_telemetry,
    v1_requests,
    v2_requests,
)

V2_INTERACTIONS = recorded_interactions(INTERACTIONS_DIR)
V1_INTERACTIONS = recorded_interactions(V1_INTERACTIONS_DIR)
SPEC_URI = "urn:microcosm-collector:read-api-v2"


@pytest.fixture(scope="module")
def stored_client():
    client = contract_client(FakeTelemetryStore())
    store_contract_telemetry(client)
    return client


def test_openapi_document_matches_the_checked_in_spec() -> None:
    response = contract_client(FakeTelemetryStore()).get("/v2/openapi.json")

    assert response.status_code == 200
    assert response.json() == load_json(OPENAPI_PATH)


def test_the_spec_describes_only_read_api_v2() -> None:
    spec = load_json(OPENAPI_PATH)

    assert set(spec["paths"]) == {"/v2/runs", "/v2/runs/{run_id}"}
    assert spec["components"]["securitySchemes"] == {
        "ReadToken": {
            "type": "apiKey",
            "description": "The dashboard read credential.",
            "in": "header",
            "name": "X-Telemetry-Read-Token",
        },
        "VercelOidcToken": {
            "type": "http",
            "description": (
                "A consumer app's Vercel OIDC token, accepted from the Vercel "
                "projects and environments this collector is configured for."
            ),
            "scheme": "bearer",
            "bearerFormat": "JWT",
        },
    }
    assert "`X-Telemetry-Read-Token`" in spec["info"]["description"]
    assert "`Authorization: Bearer`" in spec["info"]["description"]
    for operations in spec["paths"].values():
        for operation in operations.values():
            # Two requirements: either credential is enough.
            assert operation["security"] == [
                {"ReadToken": []},
                {"VercelOidcToken": []},
            ]
            assert {"200", "401", "403", "422", "503"} <= set(operation["responses"])
            assert operation["responses"]["403"]["description"] == (
                "The Vercel OIDC token is valid, but its Vercel project and "
                "environment may not read this collector."
            )
            assert operation["responses"]["403"]["content"] == {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorMessage"}
                }
            }


def test_the_spec_leaves_out_other_routes_security_schemes() -> None:
    application = create_app(
        settings=contract_settings(),
        store=FakeTelemetryStore(),
        huggingface_authenticator=ContractAuthenticator(),
    )

    @application.get(
        "/v1/other",
        dependencies=[Security(HTTPBearer(scheme_name="OtherRouteBearer"))],
    )
    def other_route() -> dict[str, str]:
        return {}

    response = TestClient(application).get("/v2/openapi.json")

    assert response.status_code == 200
    assert set(response.json()["components"]["securitySchemes"]) == {
        "ReadToken",
        "VercelOidcToken",
    }


def test_every_recorded_request_has_an_interaction(stored_client) -> None:
    assert set(V2_INTERACTIONS) == set(v2_requests(stored_client))
    assert set(V1_INTERACTIONS) == set(v1_requests())
    for name, request in v2_requests(stored_client).items():
        assert V2_INTERACTIONS[name]["request"] == request, name


@pytest.mark.parametrize("name", sorted(V2_INTERACTIONS))
def test_v2_answers_as_recorded(stored_client, name: str) -> None:
    interaction = V2_INTERACTIONS[name]

    assert answer(stored_client, interaction["request"]) == interaction["response"]


@pytest.mark.parametrize("name", sorted(V1_INTERACTIONS))
def test_v1_answers_as_before(stored_client, name: str) -> None:
    interaction = V1_INTERACTIONS[name]

    assert answer(stored_client, interaction["request"]) == interaction["response"]


def _operation_path(spec: dict[str, Any], method: str, path: str) -> str:
    request_path = urlsplit(path).path
    for template in spec["paths"]:
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$"
        operations = spec["paths"][template]
        if re.fullmatch(pattern, request_path) and method.lower() in operations:
            return template
    raise AssertionError(f"No operation in the spec for {method} {path}.")


def _json_pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


@pytest.mark.parametrize("name", sorted(V2_INTERACTIONS))
def test_recorded_answers_match_the_spec(name: str) -> None:
    spec = load_json(OPENAPI_PATH)
    request = V2_INTERACTIONS[name]["request"]
    response = V2_INTERACTIONS[name]["response"]
    template = _operation_path(spec, request["method"], request["path"])
    responses = spec["paths"][template][request["method"].lower()]["responses"]
    assert str(response["status"]) in responses
    pointer = "/".join(
        _json_pointer_part(part)
        for part in (
            "paths",
            template,
            request["method"].lower(),
            "responses",
            str(response["status"]),
            "content",
            "application/json",
            "schema",
        )
    )
    registry = Registry().with_resource(SPEC_URI, DRAFT202012.create_resource(spec))
    validator = Draft202012Validator(
        {"$ref": f"{SPEC_URI}#/{pointer}"}, registry=registry
    )

    assert [error.message for error in validator.iter_errors(response["body"])] == []


def test_postgres_answers_as_recorded(postgres_url: str) -> None:
    upgrade_database(postgres_url)
    store = PostgresTelemetryStore(postgres_url)
    try:
        client = contract_client(store)
        store_contract_telemetry(client)

        assert mismatched_interactions(client, V2_INTERACTIONS) == []
        assert mismatched_interactions(client, V1_INTERACTIONS) == []
    finally:
        store.close()


def test_a_run_answers_with_the_state_folded_from_its_events() -> None:
    for name, interaction in V2_INTERACTIONS.items():
        if not name.startswith("get-run-") or interaction["response"]["status"] != 200:
            continue
        body = interaction["response"]["body"]
        listed = {
            run["run_id"]: run
            for run in V2_INTERACTIONS["list-runs"]["response"]["body"]["runs"]
        }[body["run"]["run_id"]]

        # The list shows the stored summary; the run itself is rebuilt from its
        # events. The two agree on every field they share.
        assert {field: body["run"][field] for field in listed} == listed, name
        assert all(
            event["run_id"] == body["run"]["run_id"] for event in body["events"]
        ), name


def _utc_offsets(value: Any) -> list[timedelta]:
    """Every offset of the timestamps in a JSON document's string values."""

    if isinstance(value, dict):
        return [offset for item in value.values() for offset in _utc_offsets(item)]
    if isinstance(value, list):
        return [offset for item in value for offset in _utc_offsets(item)]
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\S+", value):
        parsed = datetime.fromisoformat(value)
        assert parsed.utcoffset() is not None, value
        return [parsed.utcoffset()]
    return []


def test_a_run_gives_every_timestamp_in_utc_whatever_offset_it_was_read_with() -> None:
    east = timezone(timedelta(hours=5))
    registered_at = datetime(2026, 10, 20, 14, 0, tzinfo=east)
    registered_run = {
        "run_id": "run-1",
        "country_code": "US",
        "pipeline": "us-fiscal-refresh",
        "candidate_id": None,
        "release_id": None,
        "run_kind": "build",
        "owner_hf_id": "hf-user-1",
        "owner_hf_username": "builder",
        "registered_at": registered_at,
    }
    events = [
        {
            "event_id": f"run-1-{sequence}",
            "run_id": "run-1",
            "producer_id": "producer-1",
            "producer_registered_at": registered_at,
            "sequence": sequence,
            "timestamp": registered_at + timedelta(minutes=sequence),
            "event_type": event_type,
            "stage_id": "calibrate",
            "status": event_status,
            "message": None,
            "details": details,
            "resources": None,
        }
        for sequence, event_type, event_status, details in (
            (1, "run", "started", {}),
            (2, "progress", "progress", {"done": 1, "total": 10}),
            (3, "heartbeat", "progress", {}),
            (4, "run", "completed", {}),
        )
    ]

    body = run_detail(registered_run, events).model_dump(mode="json")

    offsets = _utc_offsets(body)
    # registered_at, started_at, updated_at, ended_at, heartbeat_at,
    # work.updated_at and each event's timestamp.
    assert len(offsets) == 6 + len(events)
    assert set(offsets) == {timedelta(0)}
    assert body["run"]["registered_at"] == "2026-10-20T09:00:00Z"
    assert body["run"]["work"]["updated_at"] == "2026-10-20T09:02:00Z"


def test_v2_answers_503_while_a_deployment_is_in_progress() -> None:
    client = TestClient(
        create_app(
            settings=replace(contract_settings(), maintenance_mode=True),
            store=FakeTelemetryStore(),
            huggingface_authenticator=ContractAuthenticator(),
        )
    )

    for request_path in ("/v2/runs", "/v2/runs/run-1", "/v2/openapi.json"):
        response = client.get(request_path, headers=READ_HEADERS)

        assert response.status_code == 503, request_path
        assert response.headers["Retry-After"] == "60"
        assert response.json() == {
            "detail": "Telemetry service is temporarily unavailable."
        }


@pytest.mark.parametrize("request_path", ["/v1/runs", "/v2/runs", "/v2/runs/run-1"])
def test_a_credential_with_bytes_above_ascii_is_refused_as_wrong(
    request_path: str,
) -> None:
    # Starlette decodes header bytes as Latin-1, so such a credential reaches
    # the check as a string with characters above ASCII. The client returns a
    # server error as the answer it is, rather than raising it.
    client = TestClient(
        create_app(
            settings=contract_settings(),
            store=FakeTelemetryStore(),
            huggingface_authenticator=ContractAuthenticator(),
        ),
        raise_server_exceptions=False,
    )

    response = client.get(
        request_path,
        headers={"X-Telemetry-Read-Token": "caf\xe9-read-token".encode("latin-1")},
    )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "A valid dashboard read credential is required."
    }
