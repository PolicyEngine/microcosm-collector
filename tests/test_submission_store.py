"""Behaviour shared by the PostgreSQL submission store and its in-memory fake."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import create_database_engine
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.submission_store import (
    PostgresSubmissionStore,
    SubmissionNotReprocessableError,
    SubmissionOwnershipError,
    SubmissionStore,
)
from telemetry_collector.submissions import (
    MAX_JOB_ATTEMPTS,
    MAX_JOB_DISPATCHES,
    UNCLAIMED_JOB_IDLE_SECONDS,
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
        record.submission_id, "calibration-diagnostics", 1
    )
    assert accepted.processing_round == 1
    assert accepted.job_dispatch_count == 0
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


def test_recording_a_dispatch_counts_each_dispatch_number_once(
    store: SubmissionStore,
) -> None:
    job_id = accepted_job_id(store)

    store.record_dispatch(job_id, 1)
    store.record_dispatch(job_id, 1)

    dispatched = store.get_submission(derive_submission_id(submission_request()))
    assert dispatched is not None
    assert dispatched.job_dispatch_count == 1


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


def stalled(store: SubmissionStore) -> list[str]:
    return store.stalled_job_ids(idle_seconds=UNCLAIMED_JOB_IDLE_SECONDS)


def recover(store: SubmissionStore, job_id: str):
    return store.recover_job(job_id, idle_seconds=UNCLAIMED_JOB_IDLE_SECONDS)


def current_record(store: SubmissionStore):
    record = store.get_submission(derive_submission_id(submission_request()))
    assert record is not None
    return record


def test_a_dispatched_job_is_not_stalled_while_it_waits_for_its_worker(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(store)
    store.record_dispatch(job_id, 1)
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS - 1)

    assert stalled(store) == []


def test_a_queued_job_left_unclaimed_is_dispatched_again(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(store)
    store.record_dispatch(job_id, 1)
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS + 1)

    assert stalled(store) == [job_id]
    recovery = recover(store, job_id)

    assert recovery.action == "dispatch"
    assert recovery.dispatch_number == 2
    assert stalled(store) == []
    assert recover(store, job_id).action == "none"


def test_a_job_never_dispatched_is_dispatched_by_recovery(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(store)
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS + 1)

    recovery = recover(store, job_id)

    assert recovery.action == "dispatch"
    assert recovery.dispatch_number == 1


def test_a_held_lease_is_not_stalled(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(store)
    store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS - 1)

    assert stalled(store) == []
    assert recover(store, job_id).action == "none"


def test_an_expired_lease_below_the_attempt_limit_is_queued_and_dispatched(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(store)
    store.record_dispatch(job_id, 1)
    store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS + 1)

    assert stalled(store) == [job_id]
    recovery = recover(store, job_id)

    assert recovery.action == "dispatch"
    assert recovery.dispatch_number == 2
    record = current_record(store)
    assert record.status == "queued"
    assert record.job_attempts == 1
    retried = store.claim_job(job_id, holder="worker-2", lease_seconds=LEASE_SECONDS)
    assert retried is not None
    assert retried.attempt == 2


def test_an_attempt_at_the_limit_that_ends_without_finishing_fails_the_job(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(store)
    store.record_dispatch(job_id, 1)
    for _ in range(MAX_JOB_ATTEMPTS):
        # Each Cloud Run retry of the execution claims again, then is killed.
        assert store.claim_job(job_id, holder="exec-1", lease_seconds=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS + 1)

    recovery = recover(store, job_id)

    assert recovery.action == "failed"
    record = current_record(store)
    assert record.status == "failed"
    assert record.rejection is not None
    assert record.rejection["code"] == "attempts_exhausted"
    assert record.job_attempts == MAX_JOB_ATTEMPTS
    assert record.finished_at is not None
    assert stalled(store) == []


def test_a_job_that_would_need_another_dispatch_fails(
    store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(store)
    for dispatch_number in range(1, MAX_JOB_DISPATCHES + 1):
        store.record_dispatch(job_id, dispatch_number)
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS + 1)

    recovery = recover(store, job_id)

    assert recovery.action == "failed"
    record = current_record(store)
    assert record.status == "failed"
    assert record.rejection is not None
    assert record.rejection["code"] == "worker_unavailable"
    assert record.job_attempts == 0


def finished_submission(store: SubmissionStore, result: dict[str, Any]) -> str:
    job_id = accepted_job_id(store)
    store.claim_job(job_id, holder="worker-1", lease_seconds=LEASE_SECONDS)
    store.finish_job(
        job_id, holder="worker-1", status="succeeded", result=result, rejection=None
    )
    return derive_submission_id(submission_request())


def test_reprocessing_records_the_next_round_with_the_same_generations(
    store: SubmissionStore,
) -> None:
    submission_id = finished_submission(store, {"processed_by": "intake-only"})
    first = current_record(store)

    reprocessed = store.reprocess_submission(submission_id)

    assert reprocessed.status == "queued"
    assert reprocessed.processing_round == 2
    assert reprocessed.job_id == derive_job_id(
        submission_id, "calibration-diagnostics", 2
    )
    assert reprocessed.job_id != first.job_id
    assert reprocessed.job_attempts == 0
    assert reprocessed.job_dispatch_count == 0
    assert reprocessed.files == first.files
    assert reprocessed.result is None
    assert reprocessed.finished_at is None
    assert reprocessed.job_id is not None
    claimed = store.claim_job(
        reprocessed.job_id, holder="worker-2", lease_seconds=LEASE_SECONDS
    )
    assert claimed is not None
    assert claimed.attempt == 1
    assert first.job_id is not None
    assert (
        store.claim_job(first.job_id, holder="worker-3", lease_seconds=LEASE_SECONDS)
        is None
    )


def test_only_final_accepted_submissions_can_be_reprocessed(
    store: SubmissionStore,
) -> None:
    record, _ = store.create_submission(submission_request(), builder())
    with pytest.raises(SubmissionNotReprocessableError):
        store.reprocess_submission(record.submission_id)
    store.accept_submission(record.submission_id, generations())
    with pytest.raises(SubmissionNotReprocessableError):
        store.reprocess_submission(record.submission_id)
    with pytest.raises(KeyError):
        store.reprocess_submission("sub-unknown")


def test_a_submission_rejected_at_completion_cannot_be_reprocessed(
    store: SubmissionStore,
) -> None:
    record, _ = store.create_submission(submission_request(), builder())
    store.reject_submission(
        record.submission_id, {"code": "stored_object_mismatch", "message": "x"}
    )

    with pytest.raises(SubmissionNotReprocessableError):
        store.reprocess_submission(record.submission_id)


def test_reprocessable_submissions_can_be_selected_by_kind_and_intake_only(
    store: SubmissionStore,
) -> None:
    submission_id = finished_submission(store, {"processed_by": "intake-only"})

    assert store.reprocessable_submission_ids(
        "calibration-diagnostics", intake_only=True
    ) == [submission_id]
    assert store.reprocessable_submission_ids("orrery-inputs", intake_only=False) == []
    store.reprocess_submission(submission_id)
    assert (
        store.reprocessable_submission_ids("calibration-diagnostics", intake_only=False)
        == []
    )


def test_a_tree_build_result_is_not_intake_only(store: SubmissionStore) -> None:
    finished_submission(store, {"tree_build": "tree-1"})

    assert (
        store.reprocessable_submission_ids("calibration-diagnostics", intake_only=True)
        == []
    )


@pytest.fixture
def postgres_store(
    postgres_url: str, clock: SteppingClock
) -> Iterator[SubmissionStore]:
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    try:
        yield PostgresSubmissionStore(engine, clock=clock)
    finally:
        engine.dispose()


# The engine's pool holds at most five connections.
CONCURRENT_CALLERS = 4


def concurrently(call: Callable[[int], Any]) -> list[Any]:
    """Run ``call`` from several threads released at the same moment."""

    barrier = threading.Barrier(CONCURRENT_CALLERS)
    results: list[Any] = [None] * CONCURRENT_CALLERS
    errors: list[BaseException] = []

    def run(caller_index: int) -> None:
        barrier.wait()
        try:
            results[caller_index] = call(caller_index)
        except BaseException as error:  # Reported after the threads join.
            errors.append(error)

    threads = [
        threading.Thread(target=run, args=(caller_index,))
        for caller_index in range(CONCURRENT_CALLERS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    return results


def test_concurrent_completions_record_one_job_on_postgres(
    postgres_store: SubmissionStore,
) -> None:
    record, _ = postgres_store.create_submission(submission_request(), builder())

    accepted = concurrently(
        lambda caller_index: postgres_store.accept_submission(
            record.submission_id,
            {
                "calibration_diagnostics.json": f"{caller_index}1",
                "build_manifest.json": f"{caller_index}2",
            },
        )
    )

    assert len({submission.job_id for submission in accepted}) == 1
    assert len({str(submission.files) for submission in accepted}) == 1
    assert {submission.status for submission in accepted} == {"queued"}
    assert postgres_store.get_submission(record.submission_id) == accepted[0]


def test_concurrent_claims_lease_a_job_to_one_holder_on_postgres(
    postgres_store: SubmissionStore,
) -> None:
    job_id = accepted_job_id(postgres_store)

    claims = concurrently(
        lambda caller_index: postgres_store.claim_job(
            job_id, holder=f"worker-{caller_index}", lease_seconds=LEASE_SECONDS
        )
    )

    granted = [claim for claim in claims if claim is not None]
    assert len(granted) == 1
    assert granted[0].attempt == 1


def test_concurrent_recoveries_dispatch_a_stalled_job_once_on_postgres(
    postgres_store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(postgres_store)
    postgres_store.record_dispatch(job_id, 1)
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS + 1)

    recoveries = concurrently(lambda caller_index: recover(postgres_store, job_id))

    assert sorted(recovery.action for recovery in recoveries) == [
        "dispatch",
        "none",
        "none",
        "none",
    ]


def test_claims_racing_recoveries_lease_the_job_once_on_postgres(
    postgres_store: SubmissionStore, clock: SteppingClock
) -> None:
    job_id = accepted_job_id(postgres_store)
    postgres_store.claim_job(job_id, holder="exec-1", lease_seconds=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS + 1)

    def claim_or_recover(caller_index: int) -> Any:
        if caller_index % 2:
            return recover(postgres_store, job_id)
        return postgres_store.claim_job(
            job_id, holder=f"exec-{caller_index + 2}", lease_seconds=LEASE_SECONDS
        )

    outcomes = concurrently(claim_or_recover)

    claims = [outcome for index, outcome in enumerate(outcomes) if index % 2 == 0]
    recoveries = [outcome for index, outcome in enumerate(outcomes) if index % 2]
    # Whichever runs first, exactly one claim leases the expired job, and
    # at most one recovery dispatches it.
    assert len([claim for claim in claims if claim is not None]) == 1
    assert [recovery.action for recovery in recoveries].count("dispatch") <= 1
    record = current_record(postgres_store)
    assert record.job_attempts == 2
    assert record.status == "processing"
