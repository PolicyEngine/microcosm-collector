"""Persistence for artifact submissions and the worker jobs that process them.

Each submission has one job per processing round. Round 1 is recorded at
completion; an operator's reprocessing adds the next. A job counts its
attempts (worker claims) and its dispatches (Cloud Tasks, each starting one
worker execution), and recovery uses both to bring every job to a final
status, even when an attempt ends without recording one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from sqlalchemy import and_, or_, select, update
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
    FINAL_STATUSES,
    INTAKE_ONLY_HANDLER,
    MAX_JOB_ATTEMPTS,
    MAX_JOB_DISPATCHES,
    FinalStatus,
    SubmissionKind,
    SubmissionRequest,
    SubmissionStatus,
    derive_job_id,
    derive_submission_id,
    files_digest,
)

Clock = Callable[[], datetime]
ATTEMPTS_EXHAUSTED = {
    "code": "attempts_exhausted",
    "message": "The job's last attempt ended without recording a result.",
}
WORKER_UNAVAILABLE = {
    "code": "worker_unavailable",
    "message": (
        f"No worker execution finished the job within {MAX_JOB_DISPATCHES} dispatches."
    ),
}
ATTEMPT_ABANDONED = {
    "code": "attempt_abandoned",
    "message": "An attempt ended without recording a result; its lease expired.",
}
RecoveryAction = Literal["none", "dispatch", "dispatch_failed", "failed"]


class SubmissionOwnershipError(PermissionError):
    """The submission was created by another Hugging Face user."""


class SubmissionNotReprocessableError(ValueError):
    """Only a final submission with recorded object generations can be reprocessed."""


@dataclass(frozen=True)
class SubmissionRecord:
    """A submission and the state of its current job, as the API and worker see them."""

    submission_id: str
    kind: SubmissionKind
    owner_hf_id: str
    owner_hf_username: str
    licensed_data: bool
    provenance: dict[str, Any]
    files: list[dict[str, Any]]
    files_sha256: str
    status: SubmissionStatus
    rejection: dict[str, Any] | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    accepted_at: datetime | None
    finished_at: datetime | None
    processing_round: int
    job_id: str | None
    job_attempts: int
    job_dispatch_count: int


@dataclass(frozen=True)
class ClaimedJob:
    """A job leased to one worker, with the submission it processes."""

    job_id: str
    attempt: int
    submission: SubmissionRecord


@dataclass(frozen=True)
class JobRecovery:
    """What recovery did with one stalled job.

    ``dispatch`` means the job is queued again and needs dispatch
    ``dispatch_number``; ``dispatch_failed`` that the queue refused it, so a
    later recovery repeats the same dispatch.
    """

    job_id: str
    action: RecoveryAction
    dispatch_number: int | None = None


class SubmissionStore(Protocol):
    """Storage operations used by the intake endpoints, worker and operators."""

    def create_submission(
        self, request: SubmissionRequest, principal: HuggingFacePrincipal
    ) -> tuple[SubmissionRecord, bool]:
        """Record a submission, or return the existing one; True when new."""

    def get_submission(self, submission_id: str) -> SubmissionRecord | None: ...

    def accept_submission(
        self, submission_id: str, generations: Mapping[str, str]
    ) -> SubmissionRecord:
        """Record object generations and the round 1 job, once; KeyError if unknown."""

    def reject_submission(
        self, submission_id: str, rejection: dict[str, Any]
    ) -> SubmissionRecord:
        """Reject a submission still awaiting upload; KeyError if unknown.

        Used when a stored object can never match its inventory entry. A
        submission past ``awaiting_upload`` is returned unchanged.
        """

    def record_dispatch(self, job_id: str, dispatch_number: int) -> None:
        """Record that Cloud Tasks accepted dispatch ``dispatch_number``."""

    def claim_job(
        self, job_id: str, *, holder: str, lease_seconds: float
    ) -> ClaimedJob | None:
        """Lease a queued job, or one this holder or an expired lease held."""

    def finish_job(
        self,
        job_id: str,
        *,
        holder: str,
        status: FinalStatus,
        result: dict[str, Any] | None,
        rejection: dict[str, Any] | None,
    ) -> bool:
        """Record a final outcome if ``holder`` still holds the job."""

    def release_job(self, job_id: str, *, holder: str, error: dict[str, Any]) -> bool:
        """Return a held job to the queue for another attempt."""

    def stalled_job_ids(self, *, idle_seconds: float) -> list[str]:
        """Jobs whose lease expired, or queued jobs idle for ``idle_seconds``."""

    def recover_job(self, job_id: str, *, idle_seconds: float) -> JobRecovery:
        """Fail a stalled job past its limits, or queue it for another dispatch."""

    def reprocess_submission(self, submission_id: str) -> SubmissionRecord:
        """Record the next processing round's job; KeyError if unknown."""

    def reprocessable_submission_ids(
        self, kind: SubmissionKind, *, intake_only: bool
    ) -> list[str]:
        """Final, accepted submissions of a kind, optionally only intake-only ones."""


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
        "files_sha256": files_digest(request.files),
        "status": "awaiting_upload",
        "rejection": None,
        "result": None,
        "created_at": now,
        "updated_at": now,
        "accepted_at": None,
        "finished_at": None,
        "processing_round": 0,
    }


def new_job_values(
    submission_id: str, kind: str, processing_round: int, now: datetime
) -> dict[str, Any]:
    """Column values for a processing round's new, queued job."""

    return {
        "job_id": derive_job_id(submission_id, kind, processing_round),
        "submission_id": submission_id,
        "job_type": kind,
        "processing_round": processing_round,
        "status": "queued",
        "attempts": 0,
        "dispatch_count": 0,
        "dispatched_at": None,
        "lease_holder": None,
        "lease_expires_at": None,
        "last_error": None,
        "result": None,
        "created_at": now,
        "updated_at": now,
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


def job_is_claimable(
    status: str,
    lease_holder: str | None,
    lease_expires_at: datetime | None,
    *,
    holder: str,
    now: datetime,
) -> bool:
    """A queued job, or a running one this holder or an expired lease held."""

    return status == "queued" or (
        status == "running"
        and (
            lease_holder == holder
            or lease_expires_at is None
            or lease_expires_at <= now
        )
    )


def job_is_stalled(
    status: str,
    lease_expires_at: datetime | None,
    updated_at: datetime,
    *,
    now: datetime,
    idle_seconds: float,
) -> bool:
    """Whether an attempt or dispatch of the job ended without any progress.

    A running job's lease outlives its attempt, so an expired lease means the
    attempt ended without recording a result. A queued job that nothing
    claimed, released or dispatched for ``idle_seconds`` was never picked up.
    """

    if status == "running":
        return lease_expires_at is None or lease_expires_at <= now
    if status == "queued":
        return updated_at <= now - timedelta(seconds=idle_seconds)
    return False


def recovery_decision(
    attempts: int, dispatch_count: int
) -> tuple[RecoveryAction, dict[str, str] | None, int | None]:
    """For a stalled job: ``failed`` with a rejection, or the next dispatch."""

    if attempts >= MAX_JOB_ATTEMPTS:
        return "failed", ATTEMPTS_EXHAUSTED, None
    if dispatch_count >= MAX_JOB_DISPATCHES:
        return "failed", WORKER_UNAVAILABLE, None
    return "dispatch", None, dispatch_count + 1


def is_reprocessable(status: str, processing_round: int) -> bool:
    """Final, with object generations recorded at completion."""

    return status in FINAL_STATUSES and processing_round >= 1


def _record(
    submission: ArtifactSubmission, job: ArtifactJob | None
) -> SubmissionRecord:
    return SubmissionRecord(
        submission_id=submission.submission_id,
        kind=cast(SubmissionKind, submission.kind),
        owner_hf_id=submission.owner_hf_id,
        owner_hf_username=submission.owner_hf_username,
        licensed_data=submission.licensed_data,
        provenance=dict(submission.provenance),
        files=[dict(file) for file in submission.files],
        files_sha256=submission.files_sha256,
        status=cast(SubmissionStatus, submission.status),
        rejection=submission.rejection,
        result=submission.result,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
        accepted_at=submission.accepted_at,
        finished_at=submission.finished_at,
        processing_round=submission.processing_round,
        job_id=None if job is None else job.job_id,
        job_attempts=0 if job is None else job.attempts,
        job_dispatch_count=0 if job is None else job.dispatch_count,
    )


class PostgresSubmissionStore:
    """PostgreSQL submission store sharing the collector's SQLAlchemy engine.

    Whenever a transaction locks both a job and its submission, it locks the
    job first.
    """

    def __init__(self, engine: Engine, *, clock: Clock = _now) -> None:
        self._sessions: sessionmaker[Session] = create_session_factory(engine)
        self._clock = clock

    @staticmethod
    def _job_for(
        session: Session, submission: ArtifactSubmission
    ) -> ArtifactJob | None:
        if submission.processing_round == 0:
            return None
        return session.get(
            ArtifactJob,
            derive_job_id(
                submission.submission_id, submission.kind, submission.processing_round
            ),
        )

    @staticmethod
    def _locked_submission(session: Session, submission_id: str) -> ArtifactSubmission:
        submission = session.execute(
            select(ArtifactSubmission)
            .where(ArtifactSubmission.submission_id == submission_id)
            .with_for_update()
        ).scalar_one_or_none()
        if submission is None:
            raise KeyError(submission_id)
        return submission

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
            submission = self._locked_submission(session, submission_id)
            if submission.status == "awaiting_upload":
                submission.files = files_with_generations(submission.files, generations)
                submission.status = "queued"
                submission.processing_round = 1
                submission.accepted_at = now
                submission.updated_at = now
                session.execute(
                    insert(ArtifactJob)
                    .values(**new_job_values(submission_id, submission.kind, 1, now))
                    .on_conflict_do_nothing(index_elements=[ArtifactJob.job_id])
                )
                session.flush()
            return _record(submission, self._job_for(session, submission))

    def reject_submission(
        self, submission_id: str, rejection: dict[str, Any]
    ) -> SubmissionRecord:
        now = self._clock()
        with self._sessions.begin() as session:
            submission = self._locked_submission(session, submission_id)
            if submission.status == "awaiting_upload":
                submission.status = "rejected"
                submission.rejection = rejection
                submission.finished_at = now
                submission.updated_at = now
                session.flush()
            return _record(submission, self._job_for(session, submission))

    def record_dispatch(self, job_id: str, dispatch_number: int) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            session.execute(
                update(ArtifactJob)
                .where(
                    ArtifactJob.job_id == job_id,
                    ArtifactJob.dispatch_count < dispatch_number,
                    ArtifactJob.status.not_in(sorted(FINAL_STATUSES)),
                )
                .values(
                    dispatch_count=dispatch_number,
                    dispatched_at=now,
                    updated_at=now,
                )
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
            if not job_is_claimable(
                job.status,
                job.lease_holder,
                job.lease_expires_at,
                holder=holder,
                now=now,
            ):
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
        status: FinalStatus,
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
            _requeue(job, submission, now, error)
            return True

    def stalled_job_ids(self, *, idle_seconds: float) -> list[str]:
        now = self._clock()
        idle_since = now - timedelta(seconds=idle_seconds)
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(ArtifactJob.job_id)
                    .where(
                        or_(
                            and_(
                                ArtifactJob.status == "running",
                                or_(
                                    ArtifactJob.lease_expires_at.is_(None),
                                    ArtifactJob.lease_expires_at <= now,
                                ),
                            ),
                            and_(
                                ArtifactJob.status == "queued",
                                ArtifactJob.updated_at <= idle_since,
                            ),
                        )
                    )
                    .order_by(ArtifactJob.updated_at, ArtifactJob.job_id)
                )
            )

    def recover_job(self, job_id: str, *, idle_seconds: float) -> JobRecovery:
        now = self._clock()
        with self._sessions.begin() as session:
            locked = self._locked_job(session, job_id)
            if locked is None:
                return JobRecovery(job_id, "none")
            job, submission = locked
            if not job_is_stalled(
                job.status,
                job.lease_expires_at,
                job.updated_at,
                now=now,
                idle_seconds=idle_seconds,
            ):
                return JobRecovery(job_id, "none")
            action, rejection, dispatch_number = recovery_decision(
                job.attempts, job.dispatch_count
            )
            if action == "failed":
                _finish(job, submission, now, "failed", None, rejection)
                return JobRecovery(job_id, "failed")
            abandoned = ATTEMPT_ABANDONED if job.status == "running" else None
            _requeue(job, submission, now, abandoned or job.last_error)
            return JobRecovery(job_id, "dispatch", dispatch_number)

    def reprocess_submission(self, submission_id: str) -> SubmissionRecord:
        now = self._clock()
        with self._sessions.begin() as session:
            submission = self._locked_submission(session, submission_id)
            if not is_reprocessable(submission.status, submission.processing_round):
                raise SubmissionNotReprocessableError(
                    f"{submission_id} is {submission.status}; only a final "
                    "submission with recorded object generations can be "
                    "reprocessed."
                )
            next_round = submission.processing_round + 1
            session.execute(
                insert(ArtifactJob).values(
                    **new_job_values(submission_id, submission.kind, next_round, now)
                )
            )
            submission.processing_round = next_round
            submission.status = "queued"
            submission.result = None
            submission.rejection = None
            submission.finished_at = None
            submission.updated_at = now
            session.flush()
            return _record(submission, self._job_for(session, submission))

    def reprocessable_submission_ids(
        self, kind: SubmissionKind, *, intake_only: bool
    ) -> list[str]:
        query = select(ArtifactSubmission.submission_id).where(
            ArtifactSubmission.kind == kind,
            ArtifactSubmission.status.in_(sorted(FINAL_STATUSES)),
            ArtifactSubmission.processing_round >= 1,
        )
        if intake_only:
            query = query.where(
                ArtifactSubmission.result["processed_by"].astext == INTAKE_ONLY_HANDLER
            )
        with self._sessions() as session:
            return list(
                session.scalars(
                    query.order_by(
                        ArtifactSubmission.created_at, ArtifactSubmission.submission_id
                    )
                )
            )


def _requeue(
    job: ArtifactJob,
    submission: ArtifactSubmission,
    now: datetime,
    error: dict[str, Any] | None,
) -> None:
    job.status = "queued"
    job.lease_holder = None
    job.lease_expires_at = None
    job.last_error = error
    job.updated_at = now
    submission.status = "queued"
    submission.updated_at = now


def _finish(
    job: ArtifactJob,
    submission: ArtifactSubmission,
    now: datetime,
    status: FinalStatus,
    result: dict[str, Any] | None,
    rejection: dict[str, Any] | None,
) -> None:
    job.status = status
    job.lease_holder = None
    job.lease_expires_at = None
    job.last_error = rejection
    job.result = result
    job.updated_at = now
    job.finished_at = now
    submission.status = status
    submission.result = result
    submission.rejection = rejection
    submission.finished_at = now
    submission.updated_at = now
