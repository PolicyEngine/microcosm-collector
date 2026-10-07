from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
QUALIFY_SCRIPT = ROOT / "telemetry-service" / "scripts" / "qualify-deployment.sh"


class QualificationHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict[str, Any] | None]] = []
    deployment_environment = "staging"

    @classmethod
    def run_id(cls) -> str:
        return f"deployment-qualification-{cls.deployment_environment}-123-2"

    @classmethod
    def producer_id(cls) -> str:
        return f"{cls.run_id()}-producer"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return None
        return json.loads(self.rfile.read(length))

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        body = self._json_body()
        self.requests.append(("POST", self.path, body))
        if self.path == "/v1/auth/huggingface/exchange":
            assert self.headers["Authorization"] == "Bearer hf_qualification_test"
            self._respond(
                200,
                {
                    "access_token": "collector-session",
                    "token_type": "bearer",
                    "expires_in": 3600,
                },
            )
            return
        assert self.headers["Authorization"] == "Bearer collector-session"
        if self.path == "/v1/runs":
            assert body is not None
            assert body == {
                "run_id": self.run_id(),
                "producer_id": self.producer_id(),
                "country_code": "ZZ",
                "pipeline": "deployment-qualification",
                "candidate_id": None,
                "release_id": None,
                "run_kind": "qualification",
            }
            self._respond(201, {"registered": True})
            return
        if self.path == f"/v1/runs/{self.run_id()}/events":
            assert body is not None
            assert body["events"][0]["event_id"] == self.producer_id()
            assert body["events"][0]["details"] == {
                "deployment_environment": self.deployment_environment
            }
            self._respond(202, {"accepted": 1, "duplicates": 0})
            return
        self._respond(404, {"detail": "not found"})

    def do_GET(self) -> None:
        self.requests.append(("GET", self.path, None))
        assert self.headers["X-Telemetry-Read-Token"] == "read-test"
        if self.path == f"/v1/runs/{self.run_id()}":
            self._respond(
                200,
                {
                    "run_id": self.run_id(),
                    "events": [{"event_id": self.producer_id()}],
                },
            )
            return
        self._respond(404, {"detail": "not found"})


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_deployment_qualification_exercises_auth_write_and_read(
    deployment_environment: str,
) -> None:
    if shutil.which("jq") is None:
        pytest.fail("jq is required to test staging qualification")
    QualificationHandler.requests = []
    QualificationHandler.deployment_environment = deployment_environment
    server = ThreadingHTTPServer(("127.0.0.1", 0), QualificationHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["bash", str(QUALIFY_SCRIPT)],
            cwd=ROOT,
            env={
                "PATH": os.environ["PATH"],
                "COLLECTOR_URL": f"http://127.0.0.1:{server.server_port}",
                "DEPLOYMENT_ENVIRONMENT": deployment_environment,
                "TELEMETRY_HF_QUALIFICATION_TOKEN": "hf_qualification_test",
                "TELEMETRY_READ_TOKEN": "read-test",
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2",
            },
            capture_output=True,
            check=False,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"{deployment_environment} telemetry deployment qualification passed.\n"
    )
    run_id = QualificationHandler.run_id()
    assert [(method, path) for method, path, _ in QualificationHandler.requests] == [
        ("POST", "/v1/auth/huggingface/exchange"),
        ("POST", "/v1/runs"),
        ("POST", f"/v1/runs/{run_id}/events"),
        ("GET", f"/v1/runs/{run_id}"),
    ]


@pytest.mark.parametrize(
    "missing_name",
    [
        "COLLECTOR_URL",
        "DEPLOYMENT_ENVIRONMENT",
        "TELEMETRY_HF_QUALIFICATION_TOKEN",
        "TELEMETRY_READ_TOKEN",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
    ],
)
def test_deployment_qualification_requires_every_input(missing_name: str) -> None:
    environment = {
        "PATH": os.environ["PATH"],
        "COLLECTOR_URL": "http://127.0.0.1:1",
        "DEPLOYMENT_ENVIRONMENT": "staging",
        "TELEMETRY_HF_QUALIFICATION_TOKEN": "hf_qualification_test",
        "TELEMETRY_READ_TOKEN": "read-test",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
    }
    environment.pop(missing_name)
    if missing_name == "TELEMETRY_HF_QUALIFICATION_TOKEN":
        environment["HF_TOKEN"] = "must-not-be-used"

    result = subprocess.run(
        ["bash", str(QUALIFY_SCRIPT)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert f"{missing_name} is required" in result.stderr


def test_deployment_qualification_rejects_unknown_environment() -> None:
    result = subprocess.run(
        ["bash", str(QUALIFY_SCRIPT)],
        cwd=ROOT,
        env={
            "PATH": os.environ["PATH"],
            "COLLECTOR_URL": "http://127.0.0.1:1",
            "DEPLOYMENT_ENVIRONMENT": "preview",
            "TELEMETRY_HF_QUALIFICATION_TOKEN": "hf_qualification_test",
            "TELEMETRY_READ_TOKEN": "read-test",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr == "Unsupported deployment environment: preview\n"
