"""In-memory protocol fakes for HTTP contract and worker unit tests."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.intake import (
    UPLOAD_CONTENT_TYPE,
    IntakeObjectMissingError,
    PlannedUpload,
    SignedUpload,
    StoredObject,
    WorkerQueueUnavailableError,
    required_upload_headers,
)
from telemetry_collector.models import RunRegistration, TelemetryEvent
from telemetry_collector.storage import (
    RunRegistrationConflictError,
    canonical_events,
    initial_run_state,
    materialize_run,
    run_documents,
    run_summary,
)
from telemetry_collector.submission_store import (
    ATTEMPTS_EXHAUSTED,
    ClaimedJob,
    SubmissionOwnershipError,
    SubmissionRecord,
    files_with_generations,
    new_submission_values,
)
from telemetry_collector.submissions import (
    MAX_JOB_ATTEMPTS,
    FinalStatus,
    SubmissionRequest,
    derive_job_id,
)


class FakeTelemetryStore:
    """Thread-safe in-memory implementation of the store protocol."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, dict[str, dict[str, Any]]] = {}
        self._producers: dict[tuple[str, str], datetime] = {}
        self._sequences: set[tuple[str, str, int]] = set()
        self._lock = RLock()

    def register_run(
        self,
        registration: RunRegistration,
        principal: HuggingFacePrincipal,
    ) -> None:
        with self._lock:
            existing = self._runs.get(registration.run_id)
            if existing is None:
                existing = initial_run_state(registration, principal)
                self._runs[registration.run_id] = existing
                self._events[registration.run_id] = {}
            elif existing["owner_hf_id"] != principal.user_id:
                raise PermissionError("Run belongs to another Hugging Face user.")
            elif (
                existing["country_code"],
                existing["pipeline"],
                existing["candidate_id"],
                existing["release_id"],
                existing["run_kind"],
            ) != (
                registration.country_code,
                registration.pipeline,
                registration.candidate_id,
                registration.release_id,
                registration.run_kind,
            ):
                raise RunRegistrationConflictError(
                    "Run identifier is already registered with different metadata."
                )
            self._producers.setdefault(
                (registration.run_id, registration.producer_id),
                existing["registered_at"],
            )

    def append_events(
        self,
        run_id: str,
        owner_hf_id: str,
        events: Iterable[TelemetryEvent],
    ) -> tuple[int, int]:
        accepted = 0
        duplicates = 0
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            if self._runs[run_id]["owner_hf_id"] != owner_hf_id:
                raise PermissionError("Run belongs to another Hugging Face user.")
            stored = self._events[run_id]
            for model in events:
                event = model.model_dump(mode="python")
                sequence_key = (run_id, event["producer_id"], event["sequence"])
                if event["event_id"] in stored or sequence_key in self._sequences:
                    duplicates += 1
                    continue
                producer_key = (run_id, event["producer_id"])
                if producer_key not in self._producers:
                    raise PermissionError(
                        "Telemetry producer is not registered for this run."
                    )
                event["producer_registered_at"] = self._producers[producer_key]
                stored[event["event_id"]] = event
                self._sequences.add(sequence_key)
                accepted += 1
            self._runs[run_id] = materialize_run(
                self._runs[run_id], canonical_events(stored.values())
            )
        return accepted, duplicates

    def list_runs(
        self,
        *,
        country: str | None,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            runs = [
                run
                for run in self._runs.values()
                if (country is None or run["country_code"] == country)
                and (before is None or (run["registered_at"], run["run_id"]) < before)
            ]
            runs.sort(
                key=lambda run: (run["registered_at"], run["run_id"]), reverse=True
            )
            return [run_summary(deepcopy(run)) for run in runs[:limit]]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            return run_documents(
                deepcopy(run), deepcopy(list(self._events[run_id].values()))
            )

    def is_ready(self) -> bool:
        return True


class FakeSubmissionStore:
    """In-memory implementation of the submission store protocol."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._submissions: dict[str, SubmissionRecord] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def _with_job(self, record: SubmissionRecord) -> SubmissionRecord:
        job = self._jobs.get(derive_job_id(record.submission_id, record.kind))
        return deepcopy(
            replace(
                record,
                job_id=None if job is None else job["job_id"],
                job_attempts=0 if job is None else job["attempts"],
                job_enqueued=job is not None and job["enqueued_at"] is not None,
            )
        )

    def create_submission(
        self, request: SubmissionRequest, principal: HuggingFacePrincipal
    ) -> tuple[SubmissionRecord, bool]:
        with self._lock:
            values = new_submission_values(request, principal, self._clock())
            existing = self._submissions.get(values["submission_id"])
            if existing is not None:
                if existing.owner_hf_id != principal.user_id:
                    raise SubmissionOwnershipError(
                        "Submission belongs to another Hugging Face user."
                    )
                return self._with_job(existing), False
            record = SubmissionRecord(
                **values, job_id=None, job_attempts=0, job_enqueued=False
            )
            self._submissions[record.submission_id] = record
            return self._with_job(record), True

    def get_submission(self, submission_id: str) -> SubmissionRecord | None:
        with self._lock:
            record = self._submissions.get(submission_id)
            return None if record is None else self._with_job(record)

    def accept_submission(
        self, submission_id: str, generations: Mapping[str, str]
    ) -> SubmissionRecord:
        with self._lock:
            record = self._submissions[submission_id]
            if record.status == "awaiting_upload":
                now = self._clock()
                record = replace(
                    record,
                    files=files_with_generations(record.files, generations),
                    status="queued",
                    accepted_at=now,
                    updated_at=now,
                )
                self._submissions[submission_id] = record
                job_id = derive_job_id(submission_id, record.kind)
                self._jobs.setdefault(
                    job_id,
                    {
                        "job_id": job_id,
                        "submission_id": submission_id,
                        "status": "queued",
                        "attempts": 0,
                        "enqueued_at": None,
                        "lease_holder": None,
                        "lease_expires_at": None,
                        "last_error": None,
                    },
                )
            return self._with_job(record)

    def reject_submission(
        self, submission_id: str, rejection: dict[str, Any]
    ) -> SubmissionRecord:
        with self._lock:
            record = self._submissions[submission_id]
            if record.status == "awaiting_upload":
                now = self._clock()
                record = replace(
                    record,
                    status="rejected",
                    rejection=deepcopy(rejection),
                    finished_at=now,
                    updated_at=now,
                )
                self._submissions[submission_id] = record
            return self._with_job(record)

    def mark_job_enqueued(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job["enqueued_at"] is None:
                job["enqueued_at"] = self._clock()

    def _set_submission(self, job: dict[str, Any], **changes: Any) -> None:
        submission_id = job["submission_id"]
        self._submissions[submission_id] = replace(
            self._submissions[submission_id], updated_at=self._clock(), **changes
        )

    def _finish(
        self,
        job: dict[str, Any],
        status: FinalStatus,
        result: dict[str, Any] | None,
        rejection: dict[str, Any] | None,
    ) -> None:
        job.update(
            status=status,
            lease_holder=None,
            lease_expires_at=None,
            last_error=rejection,
        )
        self._set_submission(
            job,
            status=status,
            result=deepcopy(result),
            rejection=deepcopy(rejection),
            finished_at=self._clock(),
        )

    def claim_job(
        self, job_id: str, *, holder: str, lease_seconds: float
    ) -> ClaimedJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            now = self._clock()
            claimable = job["status"] == "queued" or (
                job["status"] == "running"
                and (
                    job["lease_holder"] == holder
                    or job["lease_expires_at"] is None
                    or job["lease_expires_at"] <= now
                )
            )
            if not claimable:
                return None
            if job["attempts"] >= MAX_JOB_ATTEMPTS:
                self._finish(job, "failed", None, ATTEMPTS_EXHAUSTED)
                return None
            job.update(
                status="running",
                attempts=job["attempts"] + 1,
                lease_holder=holder,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            self._set_submission(job, status="processing")
            return ClaimedJob(
                job_id=job_id,
                attempt=job["attempts"],
                submission=self._with_job(self._submissions[job["submission_id"]]),
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
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] != "running":
                return False
            if job["lease_holder"] != holder:
                return False
            self._finish(job, status, result, rejection)
            return True

    def release_job(self, job_id: str, *, holder: str, error: dict[str, Any]) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] != "running":
                return False
            if job["lease_holder"] != holder:
                return False
            job.update(
                status="queued",
                lease_holder=None,
                lease_expires_at=None,
                last_error=deepcopy(error),
            )
            self._set_submission(job, status="queued")
            return True


class FakeIntakeObjectStore:
    """In-memory intake bucket; ``upload`` stands in for a client's PUT."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self.objects: dict[str, tuple[bytes, str | None, str]] = {}
        self.signed: list[PlannedUpload] = []
        self.unreadable: set[str] = set()
        self._next_generation = 1000

    def upload(self, object_name: str, content: bytes, *, sha256: str | None) -> str:
        self._next_generation += 1
        generation = str(self._next_generation)
        self.objects[object_name] = (content, sha256, generation)
        return generation

    def sign_uploads(
        self, uploads: Sequence[PlannedUpload], *, lifetime_seconds: int
    ) -> list[SignedUpload]:
        self.signed.extend(uploads)
        expires_at = self._clock() + timedelta(seconds=lifetime_seconds)
        return [
            SignedUpload(
                name=upload.name,
                url=f"https://storage.example.test/{upload.object_name}?signed=1",
                method="PUT",
                headers={
                    "Content-Type": UPLOAD_CONTENT_TYPE,
                    **required_upload_headers(upload.size, upload.sha256),
                },
                expires_at=expires_at,
            )
            for upload in uploads
        ]

    def list_objects(self, prefix: str) -> dict[str, StoredObject]:
        return {
            name: StoredObject(
                object_name=name,
                size=len(content),
                generation=generation,
                sha256=sha256,
            )
            for name, (content, sha256, generation) in self.objects.items()
            if name.startswith(prefix)
        }

    def read_chunks(
        self, object_name: str, generation: str, *, chunk_size: int = 4
    ) -> Iterator[bytes]:
        if object_name in self.unreadable:
            raise OSError("Storage is unavailable.")
        stored = self.objects.get(object_name)
        if stored is None or stored[2] != generation:
            raise IntakeObjectMissingError(object_name)
        content = stored[0]
        for offset in range(0, len(content), chunk_size):
            yield content[offset : offset + chunk_size]


class FakeWorkerQueue:
    """Records enqueued job IDs; ``available = False`` simulates an outage."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.enqueued: list[str] = []
        self.available = True

    def enqueue(self, job_id: str) -> None:
        self.calls.append(job_id)
        if not self.available:
            raise WorkerQueueUnavailableError("The worker queue is unavailable.")
        if job_id not in self.enqueued:
            self.enqueued.append(job_id)
