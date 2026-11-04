"""Operator commands: recovering stalled jobs and reprocessing submissions."""

from __future__ import annotations

import json
from typing import Any

import pytest

from telemetry_collector import operations
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.operations import (
    recover_stalled_jobs,
    reprocess_submissions,
)
from telemetry_collector.submissions import (
    UNCLAIMED_JOB_IDLE_SECONDS,
    SubmissionRequest,
    derive_job_id,
)
from tests.fakes import FakeSubmissionStore, FakeWorkerQueue
from tests.test_submission_store import SteppingClock
from tests.test_submissions import calibration_submission, orrery_submission

LEASE_SECONDS = 600.0
QUEUE_ENVIRONMENT = {
    "COLLECTOR_WORKER_QUEUE": (
        "projects/example-project/locations/us-central1/queues/collector-intake"
    ),
    "COLLECTOR_WORKER_JOB": (
        "projects/example-project/locations/us-central1/jobs/collector-worker"
    ),
    "COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT": (
        "collector-worker-invoker@example-project.iam.gserviceaccount.com"
    ),
}


def finished(
    submissions: FakeSubmissionStore,
    submission: dict[str, Any],
    result: dict[str, Any],
) -> str:
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(submission),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )
    accepted = submissions.accept_submission(
        record.submission_id,
        {file["name"]: "1001" for file in record.files},
    )
    assert accepted.job_id is not None
    submissions.claim_job(accepted.job_id, holder="exec-1", lease_seconds=60)
    submissions.finish_job(
        accepted.job_id,
        holder="exec-1",
        status="succeeded",
        result=result,
        rejection=None,
    )
    return record.submission_id


def test_recovery_dispatches_a_stalled_job_with_the_next_dispatch_number() -> None:
    clock = SteppingClock()
    submissions = FakeSubmissionStore(clock=clock)
    worker_queue = FakeWorkerQueue()
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(calibration_submission()),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )
    accepted = submissions.accept_submission(
        record.submission_id,
        {"calibration_diagnostics.json": "1", "build_manifest.json": "2"},
    )
    assert accepted.job_id is not None
    submissions.record_dispatch(accepted.job_id, 1)
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS + 1)

    [recovery] = recover_stalled_jobs(submissions, worker_queue)

    assert recovery.action == "dispatch"
    assert worker_queue.calls == [(accepted.job_id, 2)]
    current = submissions.get_submission(record.submission_id)
    assert current is not None
    assert current.job_dispatch_count == 2


def test_recovery_retries_the_same_dispatch_when_the_queue_is_unavailable() -> None:
    clock = SteppingClock()
    submissions = FakeSubmissionStore(clock=clock)
    worker_queue = FakeWorkerQueue()
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(calibration_submission()),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )
    accepted = submissions.accept_submission(
        record.submission_id,
        {"calibration_diagnostics.json": "1", "build_manifest.json": "2"},
    )
    assert accepted.job_id is not None
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS + 1)
    worker_queue.available = False

    [unavailable] = recover_stalled_jobs(submissions, worker_queue)
    worker_queue.available = True
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS + 1)
    [retried] = recover_stalled_jobs(submissions, worker_queue)

    assert unavailable.action == "dispatch_failed"
    assert retried.action == "dispatch"
    assert worker_queue.calls == [(accepted.job_id, 1), (accepted.job_id, 1)]
    assert worker_queue.task_ids == [f"{accepted.job_id}-1"]


def test_reprocessing_dispatches_the_next_round_of_each_named_submission() -> None:
    submissions = FakeSubmissionStore()
    worker_queue = FakeWorkerQueue()
    submission_id = finished(
        submissions, calibration_submission(), {"processed_by": "intake-only"}
    )

    [outcome] = reprocess_submissions(submissions, worker_queue, [submission_id])

    next_job_id = derive_job_id(submission_id, "calibration-diagnostics", 2)
    assert outcome == {
        "submission_id": submission_id,
        "outcome": "reprocessing",
        "job_id": next_job_id,
        "processing_round": 2,
    }
    assert worker_queue.calls == [(next_job_id, 1)]
    record = submissions.get_submission(submission_id)
    assert record is not None
    assert record.status == "queued"
    assert record.job_dispatch_count == 1


def test_reprocessing_reports_a_submission_it_cannot_reprocess() -> None:
    submissions = FakeSubmissionStore()
    worker_queue = FakeWorkerQueue()
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(calibration_submission()),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )

    outcomes = reprocess_submissions(
        submissions, worker_queue, [record.submission_id, "sub-unknown"]
    )

    assert [outcome["outcome"] for outcome in outcomes] == ["refused", "not_found"]
    assert worker_queue.calls == []


def stand_in_command_services(
    monkeypatch: pytest.MonkeyPatch,
    submissions: FakeSubmissionStore,
    worker_queue: FakeWorkerQueue,
) -> dict[str, Any]:
    constructed: dict[str, Any] = {}

    class StandInEngine:
        def dispose(self) -> None:
            constructed["disposed"] = True

    def stand_in_engine(database_url: str) -> StandInEngine:
        constructed["database_url"] = database_url
        return StandInEngine()

    def stand_in_queue(**settings: str) -> FakeWorkerQueue:
        constructed["queue"] = settings
        return worker_queue

    monkeypatch.setattr(operations, "create_database_engine", stand_in_engine)
    monkeypatch.setattr(
        operations, "PostgresSubmissionStore", lambda engine: submissions
    )
    monkeypatch.setattr(operations, "CloudTasksWorkerQueue", stand_in_queue)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none")
    for name, value in QUEUE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return constructed


def test_the_reprocess_command_selects_intake_only_submissions_of_a_kind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    submissions = FakeSubmissionStore()
    worker_queue = FakeWorkerQueue()
    intake_only = finished(
        submissions, calibration_submission(), {"processed_by": "intake-only"}
    )
    finished(submissions, orrery_submission(), {"processed_by": "intake-only"})
    constructed = stand_in_command_services(monkeypatch, submissions, worker_queue)

    exit_status = operations.main(
        ["reprocess", "--kind", "calibration-diagnostics", "--intake-only"]
    )

    assert exit_status == 0
    printed = [json.loads(text) for text in capsys.readouterr().out.splitlines()]
    assert [outcome["submission_id"] for outcome in printed] == [intake_only]
    assert constructed["queue"] == {
        "queue": QUEUE_ENVIRONMENT["COLLECTOR_WORKER_QUEUE"],
        "worker_job": QUEUE_ENVIRONMENT["COLLECTOR_WORKER_JOB"],
        "invoker_service_account": QUEUE_ENVIRONMENT[
            "COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT"
        ],
    }
    assert constructed["disposed"] is True


def test_a_reprocess_dry_run_lists_without_dispatching(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    submissions = FakeSubmissionStore()
    worker_queue = FakeWorkerQueue()
    submission_id = finished(
        submissions, calibration_submission(), {"processed_by": "intake-only"}
    )
    stand_in_command_services(monkeypatch, submissions, worker_queue)

    exit_status = operations.main(
        [
            "reprocess",
            "--dry-run",
            "--kind",
            "calibration-diagnostics",
            "--intake-only",
        ]
    )

    assert exit_status == 0
    assert json.loads(capsys.readouterr().out) == {
        "submission_id": submission_id,
        "outcome": "would_reprocess",
    }
    assert worker_queue.calls == []
    record = submissions.get_submission(submission_id)
    assert record is not None
    assert record.status == "succeeded"


def test_the_reprocess_command_exits_nonzero_when_it_refuses_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stand_in_command_services(monkeypatch, FakeSubmissionStore(), FakeWorkerQueue())

    assert operations.main(["reprocess", "sub-unknown"]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "not_found"


def test_the_recover_command_reports_each_recovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = SteppingClock()
    submissions = FakeSubmissionStore(clock=clock)
    worker_queue = FakeWorkerQueue()
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(calibration_submission()),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )
    accepted = submissions.accept_submission(
        record.submission_id,
        {"calibration_diagnostics.json": "1", "build_manifest.json": "2"},
    )
    clock.advance(UNCLAIMED_JOB_IDLE_SECONDS + 1)
    stand_in_command_services(monkeypatch, submissions, worker_queue)

    assert operations.main(["recover"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "job_id": accepted.job_id,
        "action": "dispatch",
        "dispatch_number": 1,
    }


@pytest.mark.parametrize("missing_name", ["DATABASE_URL", *QUEUE_ENVIRONMENT])
def test_the_commands_require_the_database_and_worker_queue(
    monkeypatch: pytest.MonkeyPatch, missing_name: str
) -> None:
    stand_in_command_services(monkeypatch, FakeSubmissionStore(), FakeWorkerQueue())
    monkeypatch.delenv(missing_name)

    with pytest.raises(RuntimeError, match=missing_name):
        operations.main(["recover"])
