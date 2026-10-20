"""Behaviour shared by the PostgreSQL submission store and its in-memory fake."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import create_database_engine
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.submission_store import (
    PostgresSubmissionStore,
    SubmissionOwnershipError,
    SubmissionStore,
)
from telemetry_collector.submissions import (
    MAX_JOB_ATTEMPTS,
    SubmissionRequest,
    derive_job_id,
    derive_submission_id,
    files_digest,
)
from tests.fakes import FakeSubmissionStore
from tests.test_submissions import calibration_submission

LEASE_SECONDS = 600.0


class SteppingClock:
    """A clock the tests move forward explicitly."""

    def __init__(self) -> None:
        self.current = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def builder(user_id: str = "hf-user-1") -> HuggingFacePrincipal:
    return HuggingFacePrincipal(
        user_id=user_id, username="builder", organizations=("policyengine",)
    )


@pytest.fixture
def clock() -> SteppingClock:
    return SteppingClock()


@pytest.fixture(params=["memory", "postgres"])
def store(
    request: pytest.FixtureRequest, clock: SteppingClock
) -> Iterator[SubmissionStore]:
    if request.param == "memory":
        yield FakeSubmissionStore(clock=clock)
        return
    database_url = request.getfixturevalue("postgres_url")
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    try:
        yield PostgresSubmissionStore(engine, clock=clock)
    finally:
        engine.dispose()


def submission_request() -> SubmissionRequest:
    return SubmissionRequest.model_validate(calibration_submission())


def generations() -> dict[str, str]:
    return {"calibration_diagnostics.json": "1001", "build_manifest.json": "1002"}


def accepted_job_id(store: SubmissionStore) -> str:
    record, _ = store.create_submission(submission_request(), builder())
    accepted = store.accept_submission(record.submission_id, generations())
    assert accepted.job_id is not None
    return accepted.job_id


def test_creating_a_submission_records_it_awaiting_upload(
    store: SubmissionStore,
) -> None:
    request = submission_request()

    record, created = store.create_submission(request, builder())

    assert created is True
    assert record.submission_id == derive_submission_id(request)
    assert record.status == "awaiting_upload"
    assert record.kind == "calibration-diagnostics"
    assert record.owner_hf_id == "hf-user-1"
    assert record.licensed_data is True
    assert record.provenance == request.provenance.model_dump(mode="json")
    assert [file["name"] for file in record.files] == [
        "build_manifest.json",
        "calibration_diagnostics.json",
    ]
    assert record.files_sha256 == files_digest(request.files)
    assert record.job_id is None
    assert store.get_submission(record.submission_id) == record


def test_creating_the_same_submission_again_returns_the_existing_record(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    first, _ = store.create_submission(submission_request(), builder())
    clock.advance(60)

    second, created = store.create_submission(submission_request(), builder())

    assert created is False
    assert second == first


def test_another_user_cannot_create_the_same_submission(
    store: SubmissionStore,
) -> None:
    store.create_submission(submission_request(), builder())

    with pytest.raises(SubmissionOwnershipError):
        store.create_submission(submission_request(), builder("hf-user-2"))


def test_accepting_records_generations_and_a_queued_job(
    store: SubmissionStore,
) -> None:
    record, _ = store.create_submission(submission_request(), builder())

    accepted = store.accept_submission(record.submission_id, generations())

    assert accepted.status == "queued"
    assert accepted.accepted_at is not None
    assert {file["name"]: file["generation"] for file in accepted.files} == (
        generations()
    )
    assert accepted.job_id == derive_job_id(
        record.submission_id, "calibration-diagnostics"
    )
    assert accepted.job_enqueued is False
    assert accepted.job_attempts == 0


def test_accepting_again_keeps_the_first_generations(
    store: SubmissionStore,
) -> None:
    record, _ = store.create_submission(submission_request(), builder())
    first = store.accept_submission(record.submission_id, generations())

    second = store.accept_submission(
        record.submission_id,
        {"calibration_diagnostics.json": "2001", "build_manifest.json": "2002"},
    )

    assert second == first


def test_rejecting_at_completion_records_a_final_rejection_without_a_job(
    store: SubmissionStore,
) -> None:
    record, _ = store.create_submission(submission_request(), builder())
    rejection = {"code": "stored_object_mismatch", "message": "Sizes differ."}

    rejected = store.reject_submission(record.submission_id, rejection)

    assert rejected.status == "rejected"
    assert rejected.rejection == rejection
    assert rejected.finished_at is not None
    assert rejected.job_id is None
    assert store.get_submission(record.submission_id) == rejected
    assert store.accept_submission(record.submission_id, generations()) == rejected


def test_rejecting_an_accepted_submission_changes_nothing(
    store: SubmissionStore,
) -> None:
    record, _ = store.create_submission(submission_request(), builder())
    accepted = store.accept_submission(record.submission_id, generations())

    unchanged = store.reject_submission(
        record.submission_id, {"code": "stored_object_mismatch", "message": "x"}
    )

    assert unchanged == accepted


def test_rejecting_an_unknown_submission_raises_key_error(
    store: SubmissionStore,
) -> None:
    with pytest.raises(KeyError):
        store.reject_submission("sub-unknown", {"code": "x", "message": "x"})


def test_accepting_an_unknown_submission_raises_key_error(
    store: SubmissionStore,
) -> None:
    with pytest.raises(KeyError):
        store.accept_submission("sub-unknown", generations())


def test_marking_a_job_enqueued_is_recorded(store: SubmissionStore) -> None:
    record, _ = store.create_submission(submission_request(), builder())
    accepted = store.accept_submission(record.submission_id, generations())
    assert accepted.job_id is not None

    store.mark_job_enqueued(accepted.job_id)

    enqueued = store.get_submission(record.submission_id)
    assert enqueued is not None
    assert enqueued.job_enqueued is True


def test_claiming_a_queued_job_moves_its_submission_to_processing(
    store: SubmissionStore,
) -> None:
    job_id = accepted_job_id(store)

    claimed = store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)

    assert claimed is not None
    assert claimed.job_id == job_id
    assert claimed.attempt == 1
    assert claimed.submission.status == "processing"
    assert claimed.submission.files[0]["generation"] == "1002"


def test_another_holder_cannot_claim_a_leased_job(store: SubmissionStore) -> None:
    job_id = accepted_job_id(store)
    store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)

    assert (
        store.claim_job(job_id, holder="worker-2", lease_seconds=LEASE_SECONDS) is None
    )


def test_the_same_holder_claims_again_after_a_crashed_attempt(
    store: SubmissionStore,
) -> None:
    job_id = accepted_job_id(store)
    store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)

    retried = store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)

    assert retried is not None
    assert retried.attempt == 2


def test_another_holder_claims_once_the_lease_expires(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(store)
    store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS + 1)

    claimed = store.claim_job(job_id, holder="worker-2", lease_seconds=LEASE_SECONDS)

    assert claimed is not None
    assert claimed.attempt == 2


def test_finishing_a_job_records_the_final_status_and_result(
    store: SubmissionStore,
) -> None:
    job_id = accepted_job_id(store)
    claimed = store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)
    assert claimed is not None

    finished = store.finish_job(
        job_id,
        holder="worker-1",
        status="succeeded",
        result={"verified_files": 2},
        rejection=None,
    )

    assert finished is True
    record = store.get_submission(claimed.submission.submission_id)
    assert record is not None
    assert record.status == "succeeded"
    assert record.result == {"verified_files": 2}
    assert record.finished_at is not None
    assert record.job_attempts == 1
    assert (
        store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS) is None
    )


def test_only_the_current_holder_finishes_a_job(store: SubmissionStore) -> None:
    job_id = accepted_job_id(store)
    claimed = store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)
    assert claimed is not None

    finished = store.finish_job(
        job_id,
        holder="worker-2",
        status="rejected",
        result=None,
        rejection={"code": "content_mismatch", "message": "Bytes differ."},
    )

    assert finished is False
    record = store.get_submission(claimed.submission.submission_id)
    assert record is not None
    assert record.status == "processing"


def test_releasing_a_job_queues_it_for_another_attempt(
    store: SubmissionStore,
) -> None:
    job_id = accepted_job_id(store)
    store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)

    released = store.release_job(
        job_id, holder="worker-1", error={"code": "storage_unavailable"}
    )

    assert released is True
    retried = store.claim_job(job_id, holder="worker-2", lease_seconds=LEASE_SECONDS)
    assert retried is not None
    assert retried.attempt == 2


def test_a_claim_after_the_last_attempt_fails_the_job(
    store: SubmissionStore,
) -> None:
    job_id = accepted_job_id(store)
    for _ in range(MAX_JOB_ATTEMPTS):
        assert store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)

    exhausted = store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)

    assert exhausted is None
    record = store.get_submission(derive_submission_id(submission_request()))
    assert record is not None
    assert record.status == "failed"
    assert record.rejection is not None
    assert record.rejection["code"] == "attempts_exhausted"
    assert record.job_attempts == MAX_JOB_ATTEMPTS


def test_claiming_an_unknown_job_returns_nothing(store: SubmissionStore) -> None:
    assert (
        store.claim_job("job-unknown", holder="worker-1", lease_seconds=LEASE_SECONDS)
        is None
    )
    assert store.get_submission("sub-unknown") is None
