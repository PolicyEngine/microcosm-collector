"""Google Cloud Storage and Cloud Tasks implementations of the intake services.

Only the deployed collector and worker construct these. Tests use in-memory
fakes of the intake interfaces instead, so they never reach Google Cloud.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import google.auth
import requests
from google.api_core.exceptions import NotFound
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import storage

from telemetry_collector.intake import (
    UPLOAD_CONTENT_TYPE,
    IntakeObjectMissingError,
    PlannedUpload,
    SignedUpload,
    StoredObject,
    WorkerQueueUnavailableError,
    required_upload_headers,
    upload_instruction_headers,
)

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
WORKER_JOB_ID_VARIABLE = "COLLECTOR_WORKER_JOB_ID"
READ_CHUNK_BYTES = 8 * 1024 * 1024
# Each signature is one IAM signBlob call; signing in parallel keeps a
# 256-file response well inside the API's 30 s request timeout.
SIGNING_CONCURRENCY = 16
QUEUE_REQUEST_TIMEOUT_SECONDS = 10.0


def _default_credentials() -> Any:
    credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    return credentials


class GoogleCloudStorageIntake:
    """The private intake bucket, signing URLs through IAM without a key file.

    The worker only reads, so it constructs this without a signing account.
    """

    def __init__(
        self,
        *,
        bucket_name: str,
        signing_service_account: str | None = None,
        client: Any | None = None,
        credentials: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._bucket_name = bucket_name
        self._signing_service_account = signing_service_account
        self._credentials = credentials or _default_credentials()
        self._client = client or storage.Client(credentials=self._credentials)
        self._bucket = self._client.bucket(bucket_name)
        self._clock = clock

    def _access_token(self) -> str:
        if not self._credentials.valid:
            self._credentials.refresh(Request())
        return self._credentials.token

    def sign_uploads(
        self, uploads: Sequence[PlannedUpload], *, lifetime_seconds: int
    ) -> list[SignedUpload]:
        if not uploads:
            return []
        if self._signing_service_account is None:
            raise RuntimeError("Signing uploads needs a signing service account.")
        access_token = self._access_token()
        expires_at = self._clock() + timedelta(seconds=lifetime_seconds)

        def sign(upload: PlannedUpload) -> SignedUpload:
            # The library adds Content-Type and Host to the headers it is
            # given, so it gets its own copy; the instruction lists every
            # signed header explicitly instead.
            url = self._bucket.blob(upload.object_name).generate_signed_url(
                version="v4",
                method="PUT",
                expiration=timedelta(seconds=lifetime_seconds),
                content_type=UPLOAD_CONTENT_TYPE,
                headers=required_upload_headers(upload.size, upload.sha256),
                service_account_email=self._signing_service_account,
                access_token=access_token,
            )
            return SignedUpload(
                name=upload.name,
                url=url,
                method="PUT",
                headers=upload_instruction_headers(
                    upload.size, upload.sha256, urlsplit(url).netloc
                ),
                expires_at=expires_at,
            )

        with ThreadPoolExecutor(
            max_workers=min(SIGNING_CONCURRENCY, len(uploads))
        ) as executor:
            return list(executor.map(sign, uploads))

    def list_objects(self, prefix: str) -> dict[str, StoredObject]:
        listed: dict[str, StoredObject] = {}
        for blob in self._client.list_blobs(self._bucket_name, prefix=prefix):
            listed[blob.name] = StoredObject(
                object_name=blob.name,
                size=int(blob.size),
                generation=str(blob.generation),
                sha256=(blob.metadata or {}).get("sha256"),
            )
        return listed

    def read_chunks(
        self,
        object_name: str,
        generation: str,
        *,
        chunk_size: int = READ_CHUNK_BYTES,
    ) -> Iterator[bytes]:
        blob = self._bucket.blob(object_name, generation=int(generation))
        try:
            with blob.open("rb", chunk_size=chunk_size) as reader:
                while chunk := reader.read(chunk_size):
                    yield chunk
        except NotFound as error:
            raise IntakeObjectMissingError(
                f"{object_name} generation {generation} is not stored."
            ) from error


def cloud_task_id(job_id: str, dispatch_number: int) -> str:
    """The Cloud Task ID of one dispatch of a job."""

    return f"{job_id}-{dispatch_number}"


class CloudTasksWorkerQueue:
    """Creates one named Cloud Task per dispatch; the task starts the worker job.

    The task calls the Cloud Run Admin API's ``jobs.run`` with an OAuth token
    for the invoker service account, overriding the job ID variable. A task
    name can be used once, so repeating a dispatch is a no-op, and recovery
    dispatches again under the next number.
    """

    def __init__(
        self,
        *,
        queue: str,
        worker_job: str,
        invoker_service_account: str,
        session: Any | None = None,
    ) -> None:
        self._queue = queue
        self._worker_job = worker_job
        self._invoker_service_account = invoker_service_account
        self._session = session or AuthorizedSession(_default_credentials())

    def _task(self, job_id: str, dispatch_number: int) -> dict[str, Any]:
        run_request = {
            "overrides": {
                "containerOverrides": [
                    {"env": [{"name": WORKER_JOB_ID_VARIABLE, "value": job_id}]}
                ],
                "taskCount": 1,
            }
        }
        return {
            "name": f"{self._queue}/tasks/{cloud_task_id(job_id, dispatch_number)}",
            "httpRequest": {
                "httpMethod": "POST",
                "url": f"https://run.googleapis.com/v2/{self._worker_job}:run",
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(json.dumps(run_request).encode()).decode(),
                "oauthToken": {
                    "serviceAccountEmail": self._invoker_service_account,
                    "scope": CLOUD_PLATFORM_SCOPE,
                },
            },
        }

    def dispatch(self, job_id: str, dispatch_number: int) -> None:
        try:
            response = self._session.post(
                f"https://cloudtasks.googleapis.com/v2/{self._queue}/tasks",
                json={"task": self._task(job_id, dispatch_number)},
                timeout=QUEUE_REQUEST_TIMEOUT_SECONDS,
            )
        except (requests.RequestException, GoogleAuthError) as error:
            raise WorkerQueueUnavailableError(
                "The worker queue could not be reached."
            ) from error
        # 409 means a task with this name already exists: this dispatch happened.
        if response.status_code not in {200, 409}:
            raise WorkerQueueUnavailableError(
                f"The worker queue answered HTTP {response.status_code}."
            )
