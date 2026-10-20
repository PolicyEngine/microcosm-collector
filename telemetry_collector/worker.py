"""Worker framework for artifact jobs, run as one Cloud Run Job task per job.

Cloud Tasks starts an execution of the worker job with ``COLLECTOR_WORKER_JOB_ID``
set. The worker leases that job, recomputes the SHA-256 of every file at the
generation recorded at completion, then calls the handler registered for the
submission's kind. Calibration-tree and Orrery workers register their own
handlers; until then both kinds only have their bytes checked.

Run it with ``python -m telemetry_collector.worker``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from telemetry_collector.database import create_database_engine
from telemetry_collector.google_cloud import GoogleCloudStorageIntake
from telemetry_collector.intake import IntakeObjectMissingError, IntakeObjectStore
from telemetry_collector.submission_store import (
    PostgresSubmissionStore,
    SubmissionRecord,
    SubmissionStore,
)
from telemetry_collector.submissions import (
    INTAKE_ONLY_HANDLER,
    MAX_JOB_ATTEMPTS,
    intake_object_name,
)

# The worker Cloud Run Job's task timeout. A lease lasts longer, so it expires
# only after its attempt has ended, and recovery never takes a job from a
# running attempt.
WORKER_TASK_TIMEOUT_SECONDS = 60 * 60
JOB_LEASE_SECONDS = WORKER_TASK_TIMEOUT_SECONDS + 5 * 60
MAX_ERROR_MESSAGE_CHARACTERS = 500


class SubmissionRejectedError(Exception):
    """The submission's content is unusable; another attempt cannot help."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class JobContext:
    """What a handler receives: the submission and access to its files."""

    submission: SubmissionRecord
    attempt: int
    objects: IntakeObjectStore

    def files(self, role: str | None = None) -> list[dict[str, Any]]:
        """Inventory entries, optionally only those with ``role``."""

        return [
            file
            for file in self.submission.files
            if role is None or file["role"] == role
        ]

    def _file(self, name: str) -> dict[str, Any]:
        for file in self.submission.files:
            if file["name"] == name:
                return file
        raise KeyError(name)

    def read_chunks(self, name: str) -> Iterator[bytes]:
        """Stream a file's bytes at the generation recorded at completion."""

        return self.objects.read_chunks(
            intake_object_name(self.submission.submission_id, name),
            self._file(name)["generation"],
        )

    def download(self, name: str, destination: Path) -> Path:
        """Write a file to ``destination`` and return that path."""

        with destination.open("wb") as output:
            for chunk in self.read_chunks(name):
                output.write(chunk)
        return destination


JobHandler = Callable[[JobContext], dict[str, Any]]


def keep_in_intake_storage(context: JobContext) -> dict[str, Any]:
    """Default handler: the files are checked and stay in intake storage."""

    return {
        "processed_by": INTAKE_ONLY_HANDLER,
        "verified_files": len(context.files()),
    }


DEFAULT_HANDLERS: Mapping[str, JobHandler] = {
    "calibration-diagnostics": keep_in_intake_storage,
    "orrery-inputs": keep_in_intake_storage,
}


class JobOutcome(StrEnum):
    """What one worker run did with its job."""

    FINISHED = "finished"
    RETRY = "retry"
    SKIPPED = "skipped"


def verify_stored_bytes(context: JobContext) -> None:
    """Refuse the submission unless every stored file matches its inventory."""

    for file in context.files():
        digest = hashlib.sha256()
        size = 0
        try:
            for chunk in context.read_chunks(file["name"]):
                digest.update(chunk)
                size += len(chunk)
        except IntakeObjectMissingError as error:
            raise SubmissionRejectedError(
                "object_missing", f"{file['name']} is no longer stored."
            ) from error
        if size != file["size"] or digest.hexdigest() != file["sha256"]:
            raise SubmissionRejectedError(
                "content_mismatch", f"{file['name']} does not match its SHA-256."
            )


def _error_summary(error: Exception) -> dict[str, str]:
    message = f"{type(error).__name__}: {error}"
    return {"code": "worker_error", "message": message[:MAX_ERROR_MESSAGE_CHARACTERS]}


def run_job(
    job_id: str,
    *,
    submissions: SubmissionStore,
    objects: IntakeObjectStore,
    handlers: Mapping[str, JobHandler] = DEFAULT_HANDLERS,
    holder: str,
    lease_seconds: float = JOB_LEASE_SECONDS,
) -> JobOutcome:
    """Process one job once; safe to call again for the same job."""

    claimed = submissions.claim_job(job_id, holder=holder, lease_seconds=lease_seconds)
    if claimed is None:
        return JobOutcome.SKIPPED
    context = JobContext(
        submission=claimed.submission, attempt=claimed.attempt, objects=objects
    )
    try:
        verify_stored_bytes(context)
        handler = handlers.get(claimed.submission.kind)
        if handler is None:
            raise LookupError(
                f"No handler is registered for {claimed.submission.kind}."
            )
        result = handler(context)
    except SubmissionRejectedError as rejection:
        submissions.finish_job(
            job_id,
            holder=holder,
            status="rejected",
            result=None,
            rejection={"code": rejection.code, "message": rejection.message},
        )
        return JobOutcome.FINISHED
    except Exception as error:
        summary = _error_summary(error)
        if claimed.attempt >= MAX_JOB_ATTEMPTS:
            submissions.finish_job(
                job_id, holder=holder, status="failed", result=None, rejection=summary
            )
            return JobOutcome.FINISHED
        submissions.release_job(job_id, holder=holder, error=summary)
        return JobOutcome.RETRY
    submissions.finish_job(
        job_id, holder=holder, status="succeeded", result=result, rejection=None
    )
    return JobOutcome.FINISHED


def exit_code(outcome: JobOutcome) -> int:
    """Non-zero only when Cloud Run should run the task again."""

    return 1 if outcome is JobOutcome.RETRY else 0


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def main() -> int:
    """Run the job named by ``COLLECTOR_WORKER_JOB_ID``."""

    job_id = _required_environment("COLLECTOR_WORKER_JOB_ID")
    database_url = _required_environment("DATABASE_URL")
    bucket_name = _required_environment("COLLECTOR_INTAKE_BUCKET")
    # Cloud Run keeps CLOUD_RUN_EXECUTION across retries of one task, so a
    # retry continues its own lease while a duplicate execution cannot.
    holder = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex}"
    engine = create_database_engine(database_url)
    try:
        outcome = run_job(
            job_id,
            submissions=PostgresSubmissionStore(engine),
            objects=GoogleCloudStorageIntake(bucket_name=bucket_name),
            holder=holder,
        )
    finally:
        engine.dispose()
    print(json.dumps({"job_id": job_id, "outcome": outcome.value}), flush=True)
    return exit_code(outcome)


if __name__ == "__main__":
    raise SystemExit(main())
