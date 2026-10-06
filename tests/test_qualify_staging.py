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
QUALIFY_SCRIPT = ROOT / "telemetry-service" / "scripts" / "qualify-staging.sh"


class QualificationHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

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
            assert self.headers["Authorization"] == "Bearer hf_test"
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
            assert body["run_id"] == "staging-123-2"
            assert body["producer_id"] == "qualification-123-2"
            self._respond(201, {"registered": True})
            return
        if self.path == "/v1/runs/staging-123-2/events":
            assert body is not None
            assert body["events"][0]["event_id"] == "qualification-123-2"
            self._respond(202, {"accepted": 1, "duplicates": 0})
            return
        self._respond(404, {"detail": "not found"})

    def do_GET(self) -> None:
        self.requests.append(("GET", self.path, None))
        assert self.headers["X-Telemetry-Read-Token"] == "read-test"
        if self.path == "/v1/runs/staging-123-2":
            self._respond(
                200,
                {
                    "run_id": "staging-123-2",
                    "events": [{"event_id": "qualification-123-2"}],
                },
            )
            return
        self._respond(404, {"detail": "not found"})


def test_staging_qualification_exercises_auth_write_and_read() -> None:
    if shutil.which("jq") is None:
        pytest.fail("jq is required to test staging qualification")
    QualificationHandler.requests = []
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
                "HF_TOKEN": "hf_test",
                "STAGING_READ_TOKEN": "read-test",
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
    assert result.stdout == "Staging telemetry qualification passed.\n"
    assert [(method, path) for method, path, _ in QualificationHandler.requests] == [
        ("POST", "/v1/auth/huggingface/exchange"),
        ("POST", "/v1/runs"),
        ("POST", "/v1/runs/staging-123-2/events"),
        ("GET", "/v1/runs/staging-123-2"),
    ]


def test_staging_qualification_requires_every_credential() -> None:
    result = subprocess.run(
        ["bash", str(QUALIFY_SCRIPT)],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "COLLECTOR_URL is required" in result.stderr
