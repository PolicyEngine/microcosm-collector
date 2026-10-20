"""HTTP endpoints for artifact submissions.

The endpoints are mounted only while intake is on; while it is off, every
submission path answers 404 with the code ``intake_not_enabled``. File bytes
never pass through these endpoints: clients upload to signed URLs, and
completion reads only object metadata, so every request stays within the
API's 1 MiB body limit, 512 MiB of memory and 30 s timeout.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.intake import (
    IntakeServices,
    PlannedUpload,
    SignedUpload,
    StoredObject,
    WorkerQueueUnavailableError,
    dispatch_job,
)
from telemetry_collector.submission_store import (
    SubmissionOwnershipError,
    SubmissionRecord,
)
from telemetry_collector.submissions import (
    CONTRACT_VERSION,
    FINAL_STATUSES,
    UPLOAD_URL_LIFETIME_SECONDS,
    SubmissionRequest,
    intake_object_name,
    intake_object_prefix,
    receipt_digest,
)

QUEUE_RETRY_AFTER_SECONDS = 30
INTAKE_NOT_ENABLED_DETAIL = "Artifact intake is not enabled on this collector."
SUBMISSION_PATHS = ("/v1/submissions", "/v1/submissions/{submission_path:path}")
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


class IntakeProblemError(Exception):
    """An intake answer with a machine-readable ``code`` beside its ``detail``.

    The contract's client rules depend on these codes, for example to tell an
    unknown submission (``submission_not_found``) from a collector without
    intake (``intake_not_enabled``).
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        **fields: Any,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = headers
        self.fields = fields


async def intake_problem_response(request: Request, problem: Exception) -> JSONResponse:
    """Render an ``IntakeProblemError`` as ``{"detail", "code", ...}``."""

    assert isinstance(problem, IntakeProblemError)
    return JSONResponse(
        status_code=problem.status_code,
        content={"detail": problem.detail, "code": problem.code, **problem.fields},
        headers=problem.headers,
    )


def intake_not_enabled() -> IntakeProblemError:
    """The answer of a collector whose intake is switched off."""

    return IntakeProblemError(404, "intake_not_enabled", INTAKE_NOT_ENABLED_DETAIL)


def intake_not_enabled_router() -> APIRouter:
    """Answer every submission path while intake is off, outside the schema."""

    router = APIRouter()

    def refuse(submission_path: str = "") -> None:
        raise intake_not_enabled()

    for path in SUBMISSION_PATHS:
        router.add_api_route(path, refuse, methods=ALL_METHODS, include_in_schema=False)
    return router


class SubmitterClaims(Protocol):
    """The parts of a verified ``artifacts:submit`` session token used here."""

    subject: str
    username: str
    organizations: tuple[str, ...]


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _job_summary(record: SubmissionRecord) -> dict[str, Any] | None:
    if record.job_id is None:
        return None
    return {
        "job_id": record.job_id,
        "processing_round": record.processing_round,
        "attempts": record.job_attempts,
    }


def status_document(
    record: SubmissionRecord, uploads: list[SignedUpload] | None = None
) -> dict[str, Any]:
    """A submission's progress, plus upload instructions from creation."""

    document: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "submission_id": record.submission_id,
        "kind": record.kind,
        "status": record.status,
        "files_sha256": record.files_sha256,
        "created_at": _timestamp(record.created_at),
        "updated_at": _timestamp(record.updated_at),
        "accepted_at": _timestamp(record.accepted_at),
        "finished_at": _timestamp(record.finished_at),
        "job": _job_summary(record),
        "rejection": record.rejection,
    }
    if uploads is not None:
        document["uploads"] = [
            {
                "name": upload.name,
                "method": upload.method,
                "url": upload.url,
                "headers": upload.headers,
                "expires_at": upload.expires_at.isoformat(),
            }
            for upload in uploads
        ]
    return document


def receipt_document(record: SubmissionRecord) -> dict[str, Any]:
    """The final record of a submission, with its own SHA-256."""

    receipt: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "submission_id": record.submission_id,
        "kind": record.kind,
        "status": record.status,
        "licensed_data": record.licensed_data,
        "provenance": record.provenance,
        "files_sha256": record.files_sha256,
        "files": record.files,
        "submitted_by": record.owner_hf_username,
        "created_at": _timestamp(record.created_at),
        "accepted_at": _timestamp(record.accepted_at),
        "finished_at": _timestamp(record.finished_at),
        "job": _job_summary(record),
        "result": record.result,
        "rejection": record.rejection,
    }
    receipt["receipt_sha256"] = receipt_digest(receipt)
    return receipt


def storage_problems(
    record: SubmissionRecord, stored: dict[str, StoredObject]
) -> list[dict[str, str]]:
    """Inventory files whose stored object is missing or differs from it.

    ``missing`` can be fixed by uploading. ``size_mismatch`` and
    ``sha256_mismatch`` cannot, because a stored object is never replaced.
    """

    problems = []
    for file in record.files:
        stored_object = stored.get(
            intake_object_name(record.submission_id, file["name"])
        )
        if stored_object is None:
            problem = "missing"
        elif stored_object.size != file["size"]:
            problem = "size_mismatch"
        elif stored_object.sha256 != file["sha256"]:
            problem = "sha256_mismatch"
        else:
            continue
        problems.append({"name": file["name"], "problem": problem})
    return problems


def stored_object_mismatch(problems: list[dict[str, str]]) -> dict[str, str] | None:
    """The final rejection for objects that can never match, if there are any."""

    mismatched = [problem for problem in problems if problem["problem"] != "missing"]
    if not mismatched:
        return None
    listed = ", ".join(
        f"{problem['name']} ({problem['problem']})" for problem in mismatched
    )
    return {
        "code": "stored_object_mismatch",
        "message": (
            "Stored objects differ from the inventory and cannot be replaced: "
            f"{listed}."
        ),
    }


def submission_router(
    *,
    services: IntakeServices,
    submitter_claims: Callable[..., SubmitterClaims],
) -> APIRouter:
    """Create the submission endpoints over explicit services."""

    router = APIRouter(prefix="/v1/submissions")
    submissions = services.submissions
    objects = services.objects

    def owned_submission(
        submission_id: str, claims: SubmitterClaims
    ) -> SubmissionRecord:
        record = submissions.get_submission(submission_id)
        if record is None:
            raise IntakeProblemError(
                404, "submission_not_found", "Submission was not found."
            )
        if record.owner_hf_id != claims.subject:
            raise IntakeProblemError(
                403,
                "submission_owned_by_another_user",
                "Submission belongs to another Hugging Face user.",
            )
        return record

    def stored_objects(record: SubmissionRecord) -> dict[str, StoredObject]:
        return objects.list_objects(intake_object_prefix(record.submission_id))

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_submission(
        submission: SubmissionRequest,
        response: Response,
        claims: SubmitterClaims = Depends(submitter_claims),
    ) -> dict[str, Any]:
        principal = HuggingFacePrincipal(
            user_id=claims.subject,
            username=claims.username,
            organizations=claims.organizations,
        )
        try:
            record, created = submissions.create_submission(submission, principal)
        except SubmissionOwnershipError as error:
            raise IntakeProblemError(
                403, "submission_owned_by_another_user", str(error)
            ) from error
        response.status_code = 201 if created else 200
        uploads: list[SignedUpload] = []
        if record.status == "awaiting_upload":
            missing_names = {
                problem["name"]
                for problem in storage_problems(record, stored_objects(record))
                if problem["problem"] == "missing"
            }
            uploads = objects.sign_uploads(
                [
                    PlannedUpload(
                        name=file["name"],
                        object_name=intake_object_name(
                            record.submission_id, file["name"]
                        ),
                        size=file["size"],
                        sha256=file["sha256"],
                    )
                    for file in record.files
                    if file["name"] in missing_names
                ],
                lifetime_seconds=UPLOAD_URL_LIFETIME_SECONDS,
            )
        return status_document(record, uploads)

    @router.post("/{submission_id}/complete")
    def complete_submission(
        submission_id: str,
        response: Response,
        claims: SubmitterClaims = Depends(submitter_claims),
    ) -> Any:
        record = owned_submission(submission_id, claims)
        if record.status == "awaiting_upload":
            stored = stored_objects(record)
            problems = storage_problems(record, stored)
            rejection = stored_object_mismatch(problems)
            if rejection is not None:
                record = submissions.reject_submission(submission_id, rejection)
                response.status_code = 200
                return status_document(record)
            if problems:
                raise IntakeProblemError(
                    409,
                    "files_missing",
                    "Some files are not stored as declared.",
                    problems=problems,
                )
            record = submissions.accept_submission(
                submission_id,
                {
                    file["name"]: stored[
                        intake_object_name(submission_id, file["name"])
                    ].generation
                    for file in record.files
                },
            )
        if record.status == "queued" and record.job_dispatch_count == 0:
            assert record.job_id is not None, "An accepted submission has a job."
            try:
                dispatch_job(submissions, services.worker_queue, record.job_id, 1)
            except WorkerQueueUnavailableError as error:
                raise IntakeProblemError(
                    503,
                    "worker_queue_unavailable",
                    "The worker queue is unavailable; retry completion.",
                    headers={"Retry-After": str(QUEUE_RETRY_AFTER_SECONDS)},
                ) from error
            record = owned_submission(submission_id, claims)
        response.status_code = 200 if record.status in FINAL_STATUSES else 202
        return status_document(record)

    @router.get("/{submission_id}")
    def submission_status(
        submission_id: str,
        claims: SubmitterClaims = Depends(submitter_claims),
    ) -> dict[str, Any]:
        return status_document(owned_submission(submission_id, claims))

    @router.get("/{submission_id}/receipt")
    def submission_receipt(
        submission_id: str,
        claims: SubmitterClaims = Depends(submitter_claims),
    ) -> Any:
        record = owned_submission(submission_id, claims)
        if record.status not in FINAL_STATUSES:
            raise IntakeProblemError(
                409,
                "submission_not_final",
                "Submission is not final yet.",
                status=record.status,
            )
        return receipt_document(record)

    return router
