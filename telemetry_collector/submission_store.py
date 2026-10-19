"""Persistence for artifact submissions and the worker jobs that process them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import (
    ArtifactJob,
    ArtifactSubmission,
    create_session_factory,
)
from telemetry_collector.submissions import (
    MAX_JOB_ATTEMPTS,
    SubmissionRequest,
    derive_job_id,
    derive_submission_id,
    inventory_digest,
)

FinalJobStatus = Literal["succeeded", "rejected", "failed"]
Clock = Callable[[], datetime]
ATTEMPTS_EXHAUSTED = {
    "code": "attempts_exhausted",
    "message": f"The job did not finish in {MAX_JOB_ATTEMPTS} attempts.",
}


class SubmissionOwnershipError(PermissionError):
    """The submission was created by another Hugging Face user."""


@dataclass(frozen=True)
class SubmissionRecord:
    """A submission and the state of its job, as the API and worker see them."""

    submission_id: str
    kind: str
    owner_hf_id: str
    owner_hf_username: str
    licensed_data: bool
    provenance: dict[str, Any]
    files: list[dict[str, Any]]
    inventory_sha256: str
    status: str
    rejection: dict[str, Any] | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    accepted_at: datetime | None
    finished_at: datetime | None
    job_id: str | None
    job_attempts: int
    job_enqueued: bool


@dataclass(frozen=True)
class ClaimedJob:
    """A job leased to one worker, with the submission it processes."""

    job_id: str
    attempt: int
    submission: SubmissionRecord


class SubmissionStore(Protocol):
    """Storage operations used by the intake endpoints and the worker."""

    def create_submission(
        self, request: SubmissionRequest, principal: HuggingFacePrincipal
    ) -> tuple[SubmissionRecord, bool]:
        """Record a submission, or return the existing one; True when new."""

    def get_submission(self, submission_id: str) -> SubmissionRecord | None: ...

    def accept_submission(
        self, submission_id: str, generations: Mapping[str, str]
    ) -> SubmissionRecord:
        """Record object generations and a queued job, once; KeyError if unknown."""

    def mark_job_enqueued(self, job_id: str) -> None: ...

    def claim_job(
        self, job_id: str, *, holder: str, lease_seconds: float
    ) -> ClaimedJob | None:
        """Lease a queued job, or one this holder or an expired lease held."""

    def finish_job(
        self,
        job_id: str,
        *,
        holder: str,
        status: FinalJobStatus,
        result: dict[str, Any] | None,
        rejection: dict[str, Any] | None,
    ) -> bool:
        """Record a final outcome if ``holder`` still holds the job."""

    def release_job(self, job_id: str, *, holder: str, error: dict[str, Any]) -> bool:
        """Return a held job to the queue for another attempt."""


def _now() -> datetime:
    return datetime.now(UTC)


def new_submission_values(
    request: SubmissionRequest, principal: HuggingFacePrincipal, now: datetime
) -> dict[str, Any]:
    """Column values for a newly created submission."""

    return {
        "submission_id": derive_submission_id(request),
        "kind": request.kind,
        "owner_hf_id": principal.user_id,
        "owner_hf_username": principal.username,
        "licensed_data": request.licensed_data,
        "provenance": request.provenance.model_dump(mode="json"),
        "files": request.sorted_files(),
        "inventory_sha256": inventory_digest(request.files),
        "status": "awaiting_upload",
        "rejection": None,
        "result": None,
        "created_at": now,
        "updated_at": now,
        "accepted_at": None,
        "finished_at": None,
    }


def files_with_generations(
    files: list[dict[str, Any]], generations: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The inventory with each stored object's generation added."""

    missing = sorted({file["name"] for file in files} - set(generations))
    if missing:
        raise ValueError(f"No stored generation for {', '.join(missing)}.")
    return [{**file, "generation": generations[file["name"]]} for file in files]


def _record(
    submission: ArtifactSubmission, job: ArtifactJob | None
) -> SubmissionRecord:
    return SubmissionRecord(
        submission_id=submission.submission_id,
        kind=submission.kind,
        owner_hf_id=submission.owner_hf_id,
        owner_hf_username=submission.owner_hf_username,
        licensed_data=submission.licensed_data,
        provenance=dict(submission.provenance),
        files=[dict(file) for file in submission.files],
        inventory_sha256=submission.inventory_sha256,
        status=submission.status,
        rejection=submission.rejection,
        result=submission.result,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
        accepted_at=submission.accepted_at,
        finished_at=submission.finished_at,
        job_id=None if job is None else job.job_id,
        job_attempts=0 if job is None else job.attempts,
        job_enqueued=job is not None and job.enqueued_at is not None,
    )


class PostgresSubmissionStore:
    """PostgreSQL submission store sharing the collector's SQLAlchemy engine."""

    def __init__(self, engine: Engine, *, clock: Clock = _now) -> None:
        self._sessions: sessionmaker[Session] = create_session_factory(engine)
        self._clock = clock

    @staticmethod
    def _job_for(
        session: Session, submission: ArtifactSubmission
    ) -> ArtifactJob | None:
        return session.get(
            ArtifactJob, derive_job_id(submission.submission_id, submission.kind)
        )

    def create_submission(
        self, request: SubmissionRequest, principal: HuggingFacePrincipal
    ) -> tuple[SubmissionRecord, bool]:
        values = new_submission_values(request, principal, self._clock())
        with self._sessions.begin() as session:
            inserted = session.execute(
                insert(ArtifactSubmission)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[ArtifactSubmission.submission_id]
                )
                .returning(ArtifactSubmission.submission_id)
            ).scalar_one_or_none()
            submission = session.execute(
                select(ArtifactSubmission).where(
                    ArtifactSubmission.submission_id == values["submission_id"]
                )
            ).scalar_one()
            if submission.owner_hf_id != principal.user_id:
                raise SubmissionOwnershipError(
                    "Submission belongs to another Hugging Face user."
                )
            return _record(submission, self._job_for(session, submission)), (
                inserted is not None
            )

    def get_submission(self, submission_id: str) -> SubmissionRecord | None:
        with self._sessions() as session:
            submission = session.get(ArtifactSubmission, submission_id)
            if submission is None:
                return None
            return _record(submission, self._job_for(session, submission))

    def accept_submission(
        self, submission_id: str, generations: Mapping[str, str]
    ) -> SubmissionRecord:
        now = self._clock()
        with self._sessions.begin() as session:
            submission = session.execute(
                select(ArtifactSubmission)
                .where(ArtifactSubmission.submission_id == submission_id)
                .with_for_update()
            ).scalar_one_or_none()
            if submission is None:
                raise KeyError(submission_id)
            if submission.status == "awaiting_upload":
                submission.files = files_with_generations(submission.files, generations)
                submission.status = "queued"
                submission.accepted_at = now
                submission.updated_at = now
                session.execute(
                    insert(ArtifactJob)
                    .values(
                        job_id=derive_job_id(submission_id, submission.kind),
                        submission_id=submission_id,
                        job_type=submission.kind,
                        status="queued",
                        attempts=0,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=[ArtifactJob.job_id])
                )
                session.flush()
            return _record(submission, self._job_for(session, submission))

    def mark_job_enqueued(self, job_id: str) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            session.execute(
                update(ArtifactJob)
                .where(ArtifactJob.job_id == job_id, ArtifactJob.enqueued_at.is_(None))
                .values(enqueued_at=now, updated_at=now)
            )

    def _locked_job(
        self, session: Session, job_id: str
    ) -> tuple[ArtifactJob, ArtifactSubmission] | None:
        job = session.execute(
            select(ArtifactJob).where(ArtifactJob.job_id == job_id).with_for_update()
        ).scalar_one_or_none()
        if job is None:
            return None
        submission = session.execute(
            select(ArtifactSubmission)
            .where(ArtifactSubmission.submission_id == job.submission_id)
            .with_for_update()
        ).scalar_one()
        return job, submission

    def claim_job(
        self, job_id: str, *, holder: str, lease_seconds: float
    ) -> ClaimedJob | None:
        now = self._clock()
        with self._sessions.begin() as session:
            locked = self._locked_job(session, job_id)
            if locked is None:
                return None
            job, submission = locked
            claimable = job.status == "queued" or (
                job.status == "running"
                and (
                    job.lease_holder == holder
                    or job.lease_expires_at is None
                    or job.lease_expires_at <= now
                )
            )
            if not claimable:
                return None
            if job.attempts >= MAX_JOB_ATTEMPTS:
                _finish(job, submission, now, "failed", None, ATTEMPTS_EXHAUSTED)
                return None
            job.status = "running"
            job.attempts += 1
            job.lease_holder = holder
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            submission.status = "processing"
            submission.updated_at = now
            session.flush()
            return ClaimedJob(
                job_id=job.job_id,
                attempt=job.attempts,
                submission=_record(submission, job),
            )

    def finish_job(
        self,
        job_id: str,
        *,
        holder: str,
        status: FinalJobStatus,
        result: dict[str, Any] | None,
        rejection: dict[str, Any] | None,
    ) -> bool:
        now = self._clock()
        with self._sessions.begin() as session:
            locked = self._locked_job(session, job_id)
            if locked is None:
                return False
            job, submission = locked
            if job.status != "running" or job.lease_holder != holder:
                return False
            _finish(job, submission, now, status, result, rejection)
            return True

    def release_job(self, job_id: str, *, holder: str, error: dict[str, Any]) -> bool:
        now = self._clock()
        with self._sessions.begin() as session:
            locked = self._locked_job(session, job_id)
            if locked is None:
                return False
            job, submission = locked
            if job.status != "running" or job.lease_holder != holder:
                return False
            job.status = "queued"
            job.lease_holder = None
            job.lease_expires_at = None
            job.last_error = error
            job.updated_at = now
            submission.status = "queued"
            submission.updated_at = now
            return True


def _finish(
    job: ArtifactJob,
    submission: ArtifactSubmission,
    now: datetime,
    status: FinalJobStatus,
    result: dict[str, Any] | None,
    rejection: dict[str, Any] | None,
) -> None:
    job.status = status
    job.lease_holder = None
    job.lease_expires_at = None
    job.last_error = rejection
    job.updated_at = now
    submission.status = status
    submission.result = result
    submission.rejection = rejection
    submission.finished_at = now
    submission.updated_at = now
