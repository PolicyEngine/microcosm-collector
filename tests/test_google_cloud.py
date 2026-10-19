"""Google Cloud adapters, exercised with in-process stand-ins for the clients."""

from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from google.api_core.exceptions import NotFound

from telemetry_collector.google_cloud import (
    CloudTasksWorkerQueue,
    GoogleCloudStorageIntake,
)
from telemetry_collector.intake import (
    IntakeObjectMissingError,
    PlannedUpload,
    StoredObject,
    WorkerQueueUnavailableError,
)

QUEUE = "projects/example-project/locations/us-central1/queues/collector-intake"
WORKER_JOB = "projects/example-project/locations/us-central1/jobs/collector-worker"
INVOKER = "collector-worker-invoker@example-project.iam.gserviceaccount.com"
SIGNER = "collector-runtime@example-project.iam.gserviceaccount.com"


class StandInCredentials:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.token = "access-token-1" if valid else None
        self.refresh_count = 0

    def refresh(self, request: object) -> None:
        self.refresh_count += 1
        self.valid = True
        self.token = f"access-token-{self.refresh_count + 1}"


class StandInBlob:
    def __init__(
        self,
        name: str,
        *,
        content: bytes = b"",
        generation: int | None = None,
        metadata: dict[str, str] | None = None,
        missing: bool = False,
    ) -> None:
        self.name = name
        self.size = len(content)
        self.generation = generation
        self.metadata = metadata
        self.content = content
        self.missing = missing
        self.signing_arguments: dict[str, Any] | None = None

    def generate_signed_url(self, **arguments: Any) -> str:
        self.signing_arguments = arguments
        return f"https://storage.googleapis.com/collector-intake/{self.name}?signed=1"

    def open(self, mode: str, chunk_size: int) -> io.BytesIO:
        if self.missing:
            raise NotFound("No such object.")
        return io.BytesIO(self.content)


class StandInBucket:
    def __init__(self, stored: dict[str, StandInBlob]) -> None:
        self.stored = stored
        self.requested: list[tuple[str, int | None]] = []

    def blob(self, name: str, generation: int | None = None) -> StandInBlob:
        self.requested.append((name, generation))
        existing = self.stored.get(name)
        if existing is not None:
            return existing
        blob = StandInBlob(name, generation=generation, missing=True)
        self.stored[name] = blob
        return blob


class StandInStorageClient:
    def __init__(self, stored: dict[str, StandInBlob] | None = None) -> None:
        self.stored = stored or {}
        self.storage_bucket = StandInBucket(self.stored)
        self.listed: list[tuple[str, str]] = []

    def bucket(self, name: str) -> StandInBucket:
        return self.storage_bucket

    def list_blobs(self, bucket_name: str, prefix: str) -> list[StandInBlob]:
        self.listed.append((bucket_name, prefix))
        return [blob for name, blob in self.stored.items() if name.startswith(prefix)]


def storage_intake(
    client: StandInStorageClient, credentials: StandInCredentials | None = None
) -> GoogleCloudStorageIntake:
    return GoogleCloudStorageIntake(
        bucket_name="collector-intake",
        signing_service_account=SIGNER,
        client=client,
        credentials=credentials or StandInCredentials(),
        clock=lambda: datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
    )


def test_signed_uploads_pin_the_size_digest_and_single_creation() -> None:
    client = StandInStorageClient()
    planned = PlannedUpload(
        name="calibration_diagnostics.json",
        object_name="submissions/sub-1/calibration_diagnostics.json",
        size=2048,
        sha256="a" * 64,
    )

    [signed] = storage_intake(client).sign_uploads([planned], lifetime_seconds=1800)

    blob = client.stored["submissions/sub-1/calibration_diagnostics.json"]
    assert blob.signing_arguments == {
        "version": "v4",
        "method": "PUT",
        "expiration": timedelta(seconds=1800),
        "content_type": "application/octet-stream",
        "headers": {
            "x-goog-meta-sha256": "a" * 64,
            "x-goog-content-length-range": "2048,2048",
            "x-goog-if-generation-match": "0",
        },
        "service_account_email": SIGNER,
        "access_token": "access-token-1",
    }
    assert signed.name == "calibration_diagnostics.json"
    assert signed.method == "PUT"
    assert signed.url.startswith("https://storage.googleapis.com/")
    assert signed.headers == {
        "Content-Type": "application/octet-stream",
        "x-goog-meta-sha256": "a" * 64,
        "x-goog-content-length-range": "2048,2048",
        "x-goog-if-generation-match": "0",
    }
    assert signed.expires_at == datetime(2026, 10, 20, 9, 30, tzinfo=UTC)


def test_signing_refreshes_an_expired_access_token_once() -> None:
    credentials = StandInCredentials(valid=False)
    planned = [
        PlannedUpload(
            name=f"upstream-{index}.json",
            object_name=f"submissions/sub-1/upstream-{index}.json",
            size=1,
            sha256="b" * 64,
        )
        for index in range(3)
    ]

    storage_intake(StandInStorageClient(), credentials).sign_uploads(
        planned, lifetime_seconds=60
    )

    assert credentials.refresh_count == 1


def test_listing_reports_each_objects_size_generation_and_recorded_digest() -> None:
    client = StandInStorageClient(
        {
            "submissions/sub-1/a.json": StandInBlob(
                "submissions/sub-1/a.json",
                content=b"12345",
                generation=1760000000000001,
                metadata={"sha256": "c" * 64},
            ),
            "submissions/sub-1/b.json": StandInBlob(
                "submissions/sub-1/b.json", content=b"1", generation=7
            ),
            "submissions/sub-2/a.json": StandInBlob(
                "submissions/sub-2/a.json", content=b"1", generation=8
            ),
        }
    )

    listed = storage_intake(client).list_objects("submissions/sub-1/")

    assert client.listed == [("collector-intake", "submissions/sub-1/")]
    assert listed == {
        "submissions/sub-1/a.json": StoredObject(
            object_name="submissions/sub-1/a.json",
            size=5,
            generation="1760000000000001",
            sha256="c" * 64,
        ),
        "submissions/sub-1/b.json": StoredObject(
            object_name="submissions/sub-1/b.json",
            size=1,
            generation="7",
            sha256=None,
        ),
    }


def test_reading_an_object_requests_its_recorded_generation() -> None:
    client = StandInStorageClient(
        {"submissions/sub-1/a.json": StandInBlob("a", content=b"x" * 10)}
    )

    chunks = list(
        storage_intake(client).read_chunks(
            "submissions/sub-1/a.json", "42", chunk_size=4
        )
    )

    assert b"".join(chunks) == b"x" * 10
    assert client.storage_bucket.requested == [("submissions/sub-1/a.json", 42)]


def test_reading_a_missing_generation_raises_object_missing() -> None:
    with pytest.raises(IntakeObjectMissingError):
        list(
            storage_intake(StandInStorageClient()).read_chunks(
                "submissions/sub-1/gone.json", "1"
            )
        )


class StandInResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class StandInSession:
    def __init__(self, status_code: int = 200, error: Exception | None = None) -> None:
        self.status_code = status_code
        self.error = error
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def post(
        self, url: str, *, json: dict[str, Any], timeout: float
    ) -> StandInResponse:
        self.requests.append((url, json))
        if self.error is not None:
            raise self.error
        return StandInResponse(self.status_code)


def worker_queue(session: StandInSession) -> CloudTasksWorkerQueue:
    return CloudTasksWorkerQueue(
        queue=QUEUE,
        worker_job=WORKER_JOB,
        invoker_service_account=INVOKER,
        session=session,
    )


def test_enqueue_creates_a_named_task_that_runs_the_worker_job() -> None:
    session = StandInSession()

    worker_queue(session).enqueue("job-0123")

    [(url, body)] = session.requests
    assert url == f"https://cloudtasks.googleapis.com/v2/{QUEUE}/tasks"
    task = body["task"]
    assert task["name"] == f"{QUEUE}/tasks/job-0123"
    http_request = task["httpRequest"]
    assert http_request["httpMethod"] == "POST"
    assert http_request["url"] == f"https://run.googleapis.com/v2/{WORKER_JOB}:run"
    assert http_request["headers"] == {"Content-Type": "application/json"}
    assert http_request["oauthToken"] == {
        "serviceAccountEmail": INVOKER,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
    }
    assert json.loads(base64.b64decode(http_request["body"])) == {
        "overrides": {
            "containerOverrides": [
                {"env": [{"name": "COLLECTOR_WORKER_JOB_ID", "value": "job-0123"}]}
            ],
            "taskCount": 1,
        }
    }


def test_enqueue_treats_an_existing_task_as_enqueued() -> None:
    worker_queue(StandInSession(status_code=409)).enqueue("job-0123")


@pytest.mark.parametrize(
    "session",
    [
        StandInSession(status_code=503),
        StandInSession(status_code=403),
        StandInSession(error=requests.ConnectionError("unreachable")),
    ],
)
def test_enqueue_reports_an_unavailable_queue(session: StandInSession) -> None:
    with pytest.raises(WorkerQueueUnavailableError):
        worker_queue(session).enqueue("job-0123")
