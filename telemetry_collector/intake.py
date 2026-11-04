"""Artifact intake settings, and the storage and queue services intake uses.

Intake is off unless ``COLLECTOR_INTAKE_ENABLED`` is exactly ``1``. While it
is off, the collector reads none of the other intake settings, mounts no
submission endpoints, and refuses to issue ``artifacts:submit`` credentials.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from telemetry_collector.submission_store import SubmissionStore

INTAKE_SWITCH = "COLLECTOR_INTAKE_ENABLED"
SERVICE_ACCOUNT_PATTERN = (
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\.iam\.gserviceaccount\.com$"
)
BUCKET_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$"
QUEUE_PATTERN = r"^projects/[^/]+/locations/[^/]+/queues/[A-Za-z0-9-]{1,100}$"
JOB_PATTERN = r"^projects/[^/]+/locations/[^/]+/jobs/[a-z][a-z0-9-]{0,62}$"


def resource_setting(
    name: str, pattern: str, *, required_by: str = f"{INTAKE_SWITCH} is 1"
) -> str:
    """A Google Cloud resource name from the environment, checked by ``pattern``."""

    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required when {required_by}.")
    if not re.fullmatch(pattern, value):
        raise RuntimeError(f"{name} is not a valid resource name.")
    return value


@dataclass(frozen=True)
class IntakeSettings:
    """Google Cloud resources used by artifact intake."""

    bucket: str
    signing_service_account: str
    worker_queue: str
    worker_job: str
    worker_invoker_service_account: str

    @classmethod
    def from_environment(cls) -> IntakeSettings | None:
        """Return the intake settings, or ``None`` while intake is off."""

        if os.environ.get(INTAKE_SWITCH) != "1":
            return None
        return cls(
            bucket=resource_setting("COLLECTOR_INTAKE_BUCKET", BUCKET_PATTERN),
            signing_service_account=resource_setting(
                "COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT", SERVICE_ACCOUNT_PATTERN
            ),
            worker_queue=resource_setting("COLLECTOR_WORKER_QUEUE", QUEUE_PATTERN),
            worker_job=resource_setting("COLLECTOR_WORKER_JOB", JOB_PATTERN),
            worker_invoker_service_account=resource_setting(
                "COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT", SERVICE_ACCOUNT_PATTERN
            ),
        )


UPLOAD_CONTENT_TYPE = "application/octet-stream"


def required_upload_headers(size: int, sha256: str) -> dict[str, str]:
    """Headers every signed upload must carry, all covered by its signature.

    They record the declared SHA-256 as object metadata, make the storage
    service refuse a body of any other size, and allow the object to be
    created only once.
    """

    return {
        "x-goog-meta-sha256": sha256,
        "x-goog-content-length-range": f"{size},{size}",
        "x-goog-if-generation-match": "0",
    }


def upload_instruction_headers(size: int, sha256: str, host: str) -> dict[str, str]:
    """Every header a V4 signed upload's signature covers, as the client sends it.

    The signature covers ``Content-Type`` and ``Host`` as well as the required
    headers, so an upload instruction lists all five.
    """

    return {
        "Content-Type": UPLOAD_CONTENT_TYPE,
        "Host": host,
        **required_upload_headers(size, sha256),
    }


@dataclass(frozen=True)
class PlannedUpload:
    """A file the client still has to upload."""

    name: str
    object_name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SignedUpload:
    """How a client uploads one file: the URL, method and exact headers."""

    name: str
    url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True)
class StoredObject:
    """Metadata of an object in the intake bucket."""

    object_name: str
    size: int
    generation: str
    sha256: str | None


class IntakeObjectMissingError(LookupError):
    """The object, at the requested generation, is not in the intake bucket."""


class WorkerQueueUnavailableError(RuntimeError):
    """The worker queue did not accept the dispatch; it can be repeated."""


class IntakeObjectStore(Protocol):
    """The private intake bucket."""

    def sign_uploads(
        self, uploads: Sequence[PlannedUpload], *, lifetime_seconds: int
    ) -> list[SignedUpload]: ...

    def list_objects(self, prefix: str) -> dict[str, StoredObject]:
        """Every object under ``prefix``, keyed by object name."""

    def read_chunks(
        self, object_name: str, generation: str, *, chunk_size: int = ...
    ) -> Iterator[bytes]:
        """Stream one generation of an object; IntakeObjectMissingError if gone."""


class WorkerQueue(Protocol):
    """Starts one worker job execution per job ID and dispatch number."""

    def dispatch(self, job_id: str, dispatch_number: int) -> None:
        """Start an execution; repeating the same dispatch starts no second one."""


def dispatch_job(
    submissions: SubmissionStore,
    worker_queue: WorkerQueue,
    job_id: str,
    dispatch_number: int,
) -> None:
    """Dispatch a job, then record it; WorkerQueueUnavailableError if refused."""

    worker_queue.dispatch(job_id, dispatch_number)
    submissions.record_dispatch(job_id, dispatch_number)


@dataclass(frozen=True)
class IntakeServices:
    """What the submission endpoints need while intake is switched on."""

    submissions: SubmissionStore
    objects: IntakeObjectStore
    worker_queue: WorkerQueue
