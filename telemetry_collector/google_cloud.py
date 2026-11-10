"""Google Cloud Storage and Cloud Tasks implementations of the collector's services.

Only the deployed collector, worker and Orrery read service construct these:
the intake bucket and queue, and the private bucket Orrery graph files are
published to and served from. Tests use in-memory fakes of the interfaces, or stand-ins for the
Google Cloud clients, so they never reach Google Cloud.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import google.auth
import requests
from google.api_core.exceptions import (
    GoogleAPIError,
    NotFound,
    PreconditionFailed,
    RetryError,
)
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import storage
from google.cloud.storage.exceptions import DataCorruption
from google.cloud.storage.retry import DEFAULT_RETRY

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
from telemetry_collector.orrery_serving import (
    SERVED_PART_BYTES,
    OrreryObjectChangedError,
    OrreryObjectCorruptError,
    OrreryStorageUnavailableError,
    StoredGraphObject,
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


class StoredObjectConflictError(RuntimeError):
    """An object already stored under a name holds other content."""


class GoogleCloudStorageOrreryGraphs:
    """The private bucket the Orrery worker publishes graph files to.

    Every write creates its object only if no object has that name
    (``if_generation_match=0``), so a stored object is never replaced. The
    object records its SHA-256 in its metadata, as intake objects do. When an
    object already exists, its recorded SHA-256 is read back: the same digest
    keeps it, and any other digest raises ``StoredObjectConflictError``. The
    worker's account needs only to create and read objects, never to delete
    or replace them.
    """

    def __init__(self, *, bucket_name: str, client: Any | None = None) -> None:
        self._client = client or storage.Client()
        # Names the bucket without reading it, so no bucket permission is needed.
        self._bucket = self._client.bucket(bucket_name)

    def create_object(
        self, object_name: str, source: Path, *, sha256: str, content_type: str
    ) -> bool:
        """Create the object from ``source``; False when an equal one is kept."""

        blob = self._bucket.blob(object_name)
        blob.metadata = {"sha256": sha256}
        try:
            blob.upload_from_filename(
                str(source), content_type=content_type, if_generation_match=0
            )
        except PreconditionFailed:
            stored = self._bucket.get_blob(object_name)
            stored_sha256 = (
                None if stored is None else (stored.metadata or {}).get("sha256")
            )
            if stored_sha256 != sha256:
                raise StoredObjectConflictError(
                    f"{object_name} is already stored without this file's SHA-256."
                ) from None
            return False
        return True


# What the storage client raises when the bucket does not answer as expected;
# RetryError, raised once a call's retry deadline has passed, is a
# GoogleAPIError.
STORAGE_ERRORS: tuple[type[Exception], ...] = (
    GoogleAPIError,
    GoogleAuthError,
    requests.RequestException,
)
#: How long the Orrery read service waits on each Cloud Storage request, to
#: connect and for each part of its answer, instead of the client's 60
#: seconds.
ORRERY_STORAGE_TIMEOUT_SECONDS = 10.0
#: How long it retries one call, instead of the client's 120 seconds, before
#: the call fails and the request answers 503. A fetch makes several calls one
#: after another, so one call's whole time, this and a last request's
#: timeout, stays under a quarter of the read service's request timeout
#: (``telemetry_collector.orrery_read_app.REQUEST_TIMEOUT_SECONDS``).
ORRERY_STORAGE_RETRY_SECONDS = 30.0
#: The client's retry rule (which errors to retry, and how long to wait
#: between attempts) with that deadline.
ORRERY_STORAGE_RETRY = DEFAULT_RETRY.with_timeout(ORRERY_STORAGE_RETRY_SECONDS)


def _storage_error_type(error: Exception) -> str:
    """The class name of a storage error, and of the last error a retry gave
    up on, never the message, which can quote the request."""

    if isinstance(error, RetryError) and error.cause is not None:
        return f"{type(error).__name__} after {type(error.cause).__name__}"
    return type(error).__name__


class GoogleCloudStorageOrreryGraphReader:
    """The private Orrery bucket, as the Orrery read service reads it to serve
    Orrery graph files.

    The read service runs as the collector's runtime account, which has
    ``roles/storage.objectViewer`` on the bucket, so it can list and read
    objects, and can neither write, replace nor delete one. Every call passes
    ``ORRERY_STORAGE_TIMEOUT_SECONDS`` and ``ORRERY_STORAGE_RETRY``. An object
    is read at the generation it was found at, in ranged requests of
    ``part_bytes`` each (the library's ``BlobReader``, opened with that chunk
    size, which passes the timeout and the retry to each request), so no read
    holds more than one part. A generation no longer stored raises
    ``OrreryObjectChangedError``; a checksum failure the client reports
    (``DataCorruption``) raises ``OrreryObjectCorruptError``, though the
    client doesn't checksum ranged reads, so the service's own SHA-256 is the
    check that matters; any other error raises
    ``OrreryStorageUnavailableError``, naming the object or folder and the
    error's type.
    """

    def __init__(
        self,
        *,
        bucket_name: str,
        client: Any | None = None,
        part_bytes: int = SERVED_PART_BYTES,
    ) -> None:
        self._bucket_name = bucket_name
        self._client = client or storage.Client()
        # Names the bucket without reading it, so no bucket permission is needed.
        self._bucket = self._client.bucket(bucket_name)
        self._part_bytes = part_bytes

    def list_object_names(self, prefix: str) -> list[str]:
        try:
            # The listing's pages are fetched while it is iterated, each with
            # the timeout and the retry.
            return [
                blob.name
                for blob in self._client.list_blobs(
                    self._bucket_name,
                    prefix=prefix,
                    timeout=ORRERY_STORAGE_TIMEOUT_SECONDS,
                    retry=ORRERY_STORAGE_RETRY,
                )
            ]
        except STORAGE_ERRORS as error:
            raise OrreryStorageUnavailableError(
                f"{prefix} could not be listed in the Orrery bucket.",
                object_name=prefix,
                error_type=_storage_error_type(error),
            ) from error

    def find_object(self, object_name: str) -> StoredGraphObject | None:
        try:
            blob = self._bucket.get_blob(
                object_name,
                timeout=ORRERY_STORAGE_TIMEOUT_SECONDS,
                retry=ORRERY_STORAGE_RETRY,
            )
        except NotFound:
            return None
        except STORAGE_ERRORS as error:
            raise OrreryStorageUnavailableError(
                f"{object_name} could not be looked up in the Orrery bucket.",
                object_name=object_name,
                error_type=_storage_error_type(error),
            ) from error
        if blob is None:
            return None
        return StoredGraphObject(name=object_name, generation=int(blob.generation))

    def read_parts(self, stored: StoredGraphObject) -> Iterator[bytes]:
        blob = self._bucket.blob(stored.name, generation=stored.generation)
        try:
            with blob.open(
                "rb",
                chunk_size=self._part_bytes,
                timeout=ORRERY_STORAGE_TIMEOUT_SECONDS,
                retry=ORRERY_STORAGE_RETRY,
            ) as reader:
                while part := reader.read(self._part_bytes):
                    yield part
        except NotFound as error:
            raise OrreryObjectChangedError(
                f"{stored.name} generation {stored.generation} is no longer stored."
            ) from error
        except DataCorruption as error:
            raise OrreryObjectCorruptError(
                f"{stored.name} generation {stored.generation} did not match its "
                "checksum."
            ) from error
        except STORAGE_ERRORS as error:
            raise OrreryStorageUnavailableError(
                f"{stored.name} could not be read from the Orrery bucket.",
                object_name=stored.name,
                error_type=_storage_error_type(error),
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
