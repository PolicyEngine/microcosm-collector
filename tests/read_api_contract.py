"""Recorded read API interactions, shared by the contract tests and their tool.

``contracts/read-api-v2/telemetry.json`` lists the telemetry a collector
receives, and each file in ``contracts/read-api-v2/interactions`` records one
read API v2 request and the collector's answer to it once that telemetry is
stored. The TypeScript client's tests answer its requests with the same files.
``tests/fixtures/read_api_v1`` records version 1's answers from the same
telemetry, so a change to version 1, which the dashboard reads, fails a test.

``tools/update-read-api-contract.py`` writes the files from the code; the tests
check that the code still gives exactly those answers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from telemetry_collector import storage
from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.storage import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts" / "read-api-v2"
OPENAPI_PATH = CONTRACT_DIR / "openapi.json"
TELEMETRY_PATH = CONTRACT_DIR / "telemetry.json"
INTERACTIONS_DIR = CONTRACT_DIR / "interactions"
V1_INTERACTIONS_DIR = ROOT / "tests" / "fixtures" / "read_api_v1"

READ_TOKEN = "dashboard-read-token"
READ_HEADERS = {"X-Telemetry-Read-Token": READ_TOKEN}

RUN_IDS = (
    "us-run-completed",
    "us-run-failed",
    "gb-run-blocked",
    "gb-run-restarted",
    "us-run-registered",
    "us-run-with-readings",
)


class ContractAuthenticator:
    """Accepts any Hugging Face token as one organization member."""

    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        return HuggingFacePrincipal(
            user_id="hf-user-1",
            username="builder",
            organizations=(required_org,),
        )


def contract_settings() -> CollectorSettings:
    return CollectorSettings(
        jwt_secret="contract-secret-that-is-long-enough",
        read_token=READ_TOKEN,
    )


def contract_client(store: TelemetryStore) -> TestClient:
    return TestClient(
        create_app(
            settings=contract_settings(),
            store=store,
            huggingface_authenticator=ContractAuthenticator(),
        )
    )


@contextmanager
def registration_clock() -> Iterator[Callable[[datetime], None]]:
    """Let the caller set the time the store records for each registration."""

    current: list[datetime] = []
    original = storage._now
    storage._now = lambda: current[-1]
    try:
        yield current.append
    finally:
        storage._now = original


def load_json(source_path: Path) -> Any:
    return json.loads(source_path.read_text(encoding="utf-8"))


def write_json(target_path: Path, value: Any) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def store_contract_telemetry(client: TestClient) -> None:
    """Register runs and post events through the write API, as builds do."""

    exchange = client.post(
        "/v1/auth/huggingface/exchange",
        headers={"Authorization": "Bearer hf_contract"},
    )
    assert exchange.status_code == 200, exchange.text
    authorization = {"Authorization": f"Bearer {exchange.json()['access_token']}"}
    with registration_clock() as set_time:
        for step in load_json(TELEMETRY_PATH)["steps"]:
            if "registration" in step:
                set_time(datetime.fromisoformat(step["register_at"]))
                response = client.post(
                    "/v1/runs", headers=authorization, json=step["registration"]
                )
                assert response.status_code == 201, response.text
            else:
                response = client.post(
                    f"/v1/runs/{step['run_id']}/events",
                    headers=authorization,
                    json={"events": step["events"]},
                )
                assert response.status_code == 202, response.text
                assert response.json()["duplicates"] == 0, response.text


def answer(client: TestClient, request: dict[str, Any]) -> dict[str, Any]:
    response = client.request(
        request["method"], request["path"], headers=request.get("headers", {})
    )
    return {"status": response.status_code, "body": response.json()}


def v2_requests(client: TestClient) -> dict[str, dict[str, Any]]:
    """Name each recorded v2 request; paging requests follow earlier answers."""

    def read(path: str) -> dict[str, Any]:
        return {"method": "GET", "path": path, "headers": READ_HEADERS}

    requests = {
        "list-runs": read("/v2/runs"),
        "list-runs-gb-first-page": read("/v2/runs?country=GB&limit=1"),
    }
    cursor = answer(client, requests["list-runs-gb-first-page"])["body"].get(
        "next_before"
    )
    requests["list-runs-gb-second-page"] = read(
        f"/v2/runs?country=GB&limit=1&before={cursor}"
    )
    cursor = answer(client, requests["list-runs-gb-second-page"])["body"].get(
        "next_before"
    )
    requests["list-runs-gb-after-the-last-page"] = read(
        f"/v2/runs?country=GB&limit=1&before={cursor}"
    )
    requests["list-runs-of-a-country-without-runs"] = read("/v2/runs?country=FR")
    for run_id in RUN_IDS:
        requests[f"get-run-{run_id}"] = read(f"/v2/runs/{run_id}")
    requests["get-run-not-found"] = read("/v2/runs/no-such-run")
    requests["get-run-with-an-invalid-id"] = read("/v2/runs/not%20a%20run%20id")
    requests["list-runs-without-a-credential"] = {"method": "GET", "path": "/v2/runs"}
    requests["list-runs-with-a-wrong-credential"] = {
        "method": "GET",
        "path": "/v2/runs",
        "headers": {"X-Telemetry-Read-Token": "not-the-read-token"},
    }
    # The contract's collector has no Vercel OIDC settings, so it refuses every
    # Vercel OIDC token, whatever the token holds.
    requests["list-runs-with-a-bearer-token-while-vercel-oidc-is-off"] = {
        "method": "GET",
        "path": "/v2/runs",
        "headers": {"Authorization": "Bearer not-a-vercel-oidc-token"},
    }
    requests["list-runs-with-an-invalid-cursor"] = read("/v2/runs?before=not-a-cursor")
    requests["list-runs-with-an-invalid-limit"] = read("/v2/runs?limit=0")
    return requests


def v1_requests() -> dict[str, dict[str, Any]]:
    requests = {
        "list-runs": {"method": "GET", "path": "/v1/runs", "headers": READ_HEADERS}
    }
    for run_id in RUN_IDS:
        requests[f"get-run-{run_id}"] = {
            "method": "GET",
            "path": f"/v1/runs/{run_id}",
            "headers": READ_HEADERS,
        }
    return requests


def recorded_interactions(directory: Path) -> dict[str, dict[str, Any]]:
    return {
        interaction_path.stem: load_json(interaction_path)
        for interaction_path in sorted(directory.glob("*.json"))
    }


def mismatched_interactions(
    client: TestClient, interactions: dict[str, dict[str, Any]]
) -> list[str]:
    """Name every recorded interaction whose request the collector answers differently."""

    return [
        name
        for name, interaction in interactions.items()
        if answer(client, interaction["request"]) != interaction["response"]
    ]
