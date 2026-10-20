"""HTTP endpoints for artifact submissions, mounted only while intake is on.

File bytes never pass through these endpoints: clients upload to signed URLs,
and completion reads only object metadata, so every request stays within the
API's 1 MiB body limit, 512 MiB of memory and 30 s timeout.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.intake import (
    IntakeServices,
    PlannedUpload,
    SignedUpload,
    StoredObject,
    WorkerQueueUnavailableError,
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


class SubmitterClaims(Protocol):
    """The parts of a verified ``artifacts:submit`` session token used here."""

    subject: str
    username: str
    organizations: tuple[str, ...]


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


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
        "job": {"job_id": record.job_id, "attempts": record.job_attempts},
        "result": record.result,
        "rejection": record.rejection,
    }
    receipt["receipt_sha256"] = receipt_digest(receipt)
    return receipt


def storage_problems(
    record: SubmissionRecord, stored: dict[str, StoredObject]
) -> list[dict[str, str]]:
    """Inventory files whose stored object is missing or differs from it."""

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
            raise HTTPException(status_code=404, detail="Submission was not found.")
        if record.owner_hf_id != claims.subject:
            raise HTTPException(
                status_code=403,
                detail="Submission belongs to another Hugging Face user.",
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
            raise HTTPException(status_code=403, detail=str(error)) from error
        response.status_code = 201 if created else 200
        uploads: list[SignedUpload] = []
        if record.status == "awaiting_upload":
            missing_names = {
                problem["name"]
                for problem in storage_problems(record, stored_objects(record))
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
            if problems:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "Some files are not stored as declared.",
                        "problems": problems,
                    },
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
        if record.status == "queued" and not record.job_enqueued:
            assert record.job_id is not None, "An accepted submission has a job."
            try:
                services.worker_queue.enqueue(record.job_id)
            except WorkerQueueUnavailableError as error:
                raise HTTPException(
                    status_code=503,
                    detail="The worker queue is unavailable; retry completion.",
                    headers={"Retry-After": str(QUEUE_RETRY_AFTER_SECONDS)},
                ) from error
            submissions.mark_job_enqueued(record.job_id)
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
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Submission is not final yet.",
                    "status": record.status,
                },
            )
        return receipt_document(record)

    return router
