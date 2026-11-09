"""Write read API v2's OpenAPI document and the recorded interactions from the code.

Run it after an intended change to the read API, review the changed files, and
then regenerate the TypeScript client's types (see packages/collector-client):

    uv run python tools/update-read-api-contract.py

It stores contracts/read-api-v2/telemetry.json in an in-memory collector, then
records each request in tests/read_api_contract.py with the collector's answer:
read API v2's in contracts/read-api-v2/interactions, and version 1's in
tests/fixtures/read_api_v1.
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fakes import FakeTelemetryStore  # noqa: E402
from tests.read_api_contract import (  # noqa: E402
    INTERACTIONS_DIR,
    OPENAPI_PATH,
    V1_INTERACTIONS_DIR,
    answer,
    contract_client,
    store_contract_telemetry,
    v1_requests,
    v2_requests,
    write_json,
)


def record(client, requests, directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    for name, request in requests.items():
        write_json(
            directory / f"{name}.json",
            {"request": request, "response": answer(client, request)},
        )


def main() -> None:
    client = contract_client(FakeTelemetryStore())
    write_json(OPENAPI_PATH, client.get("/v2/openapi.json").json())
    store_contract_telemetry(client)
    record(client, v2_requests(client), INTERACTIONS_DIR)
    record(client, v1_requests(), V1_INTERACTIONS_DIR)


if __name__ == "__main__":
    main()
