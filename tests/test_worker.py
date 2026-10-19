from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from telemetry_collector import worker
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.submissions import (
    MAX_JOB_ATTEMPTS,
    SubmissionRequest,
    intake_object_name,
)
from telemetry_collector.worker import (
    DEFAULT_HANDLERS,
    JobContext,
    JobOutcome,
    SubmissionRejectedError,
    exit_code,
    run_job,
)
from tests.fakes import FakeIntakeObjectStore, FakeSubmissionStore
from tests.test_submissions import calibration_submission

DIAGNOSTICS_BYTES = b'{"schema_version": 8, "targets": []}'
MANIFEST_BYTES = b'{"build": "uk-frs-calibration-attempt-1"}'
CONTENTS = {
    "calibration_diagnostics.json": DIAGNOSTICS_BYTES,
    "build_manifest.json": MANIFEST_BYTES,
}


@dataclass
class AcceptedSubmission:
    submissions: FakeSubmissionStore
    objects: FakeIntakeObjectStore
    submission_id: str
    job_id: str

    def run(self, handlers: dict[str, Any] | None = None, holder: str = "exec-1"):
        return run_job(
            self.job_id,
            submissions=self.submissions,
            objects=self.objects,
            handlers=DEFAULT_HANDLERS if handlers is None else handlers,
            holder=holder,
        )

    def record(self):
        record = self.submissions.get_submission(self.submission_id)
        assert record is not None
        return record


def accepted_submission(
    stored_contents: dict[str, bytes] | None = None,
) -> AcceptedSubmission:
    submission = calibration_submission()
    for file in submission["files"]:
        file["size"] = len(CONTENTS[file["name"]])
        file["sha256"] = hashlib.sha256(CONTENTS[file["name"]]).hexdigest()
    submissions = FakeSubmissionStore()
    objects = FakeIntakeObjectStore()
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(submission),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )
    generations = {}
    for file in record.files:
        content = (stored_contents or CONTENTS)[file["name"]]
        generations[file["name"]] = objects.upload(
            intake_object_name(record.submission_id, file["name"]),
            content,
            sha256=file["sha256"],
        )
    accepted = submissions.accept_submission(record.submission_id, generations)
    assert accepted.job_id is not None
    return AcceptedSubmission(
        submissions, objects, record.submission_id, accepted.job_id
    )


def test_both_kinds_have_a_handler_that_keeps_files_in_intake_storage() -> None:
    accepted = accepted_submission()

    outcome = accepted.run()

    assert set(DEFAULT_HANDLERS) == {"calibration-diagnostics", "orrery-inputs"}
    assert outcome is JobOutcome.FINISHED
    assert accepted.record().status == "succeeded"
    assert accepted.record().result == {
        "processed_by": "intake-only",
        "verified_files": 2,
    }


def test_a_verified_submission_finishes_with_the_handler_result() -> None:
    accepted = accepted_submission()
    seen_attempts: list[int] = []

    def build_tree(context: JobContext) -> dict[str, Any]:
        seen_attempts.append(context.attempt)
        return {"tree_build": "tree-1"}

    accepted.run({"calibration-diagnostics": build_tree})

    assert accepted.record().status == "succeeded"
    assert accepted.record().result == {"tree_build": "tree-1"}
    assert seen_attempts == [1]


def test_handlers_read_files_by_role_and_download_them(tmp_path: Path) -> None:
    accepted = accepted_submission()
    downloaded: dict[str, bytes] = {}

    def read_diagnostics(context: JobContext) -> dict[str, Any]:
        [diagnostics] = context.files("calibration_diagnostics")
        destination = context.download(diagnostics["name"], tmp_path / "input.json")
        downloaded["bytes"] = destination.read_bytes()
        streamed = b"".join(context.read_chunks("build_manifest.json"))
        return {"manifest_bytes": len(streamed)}

    accepted.run({"calibration-diagnostics": read_diagnostics})

    assert downloaded["bytes"] == DIAGNOSTICS_BYTES
    assert accepted.record().result == {"manifest_bytes": len(MANIFEST_BYTES)}


def test_stored_bytes_that_differ_from_the_inventory_reject_the_submission() -> None:
    altered = DIAGNOSTICS_BYTES.replace(b"8", b"9")
    accepted = accepted_submission(
        {"calibration_diagnostics.json": altered, "build_manifest.json": MANIFEST_BYTES}
    )
    handler_calls: list[str] = []

    outcome = accepted.run(
        {"calibration-diagnostics": lambda context: handler_calls.append("called")}
    )

    assert outcome is JobOutcome.FINISHED
    assert handler_calls == []
    record = accepted.record()
    assert record.status == "rejected"
    assert record.rejection == {
        "code": "content_mismatch",
        "message": "calibration_diagnostics.json does not match its SHA-256.",
    }


def test_a_missing_object_rejects_the_submission() -> None:
    accepted = accepted_submission()
    del accepted.objects.objects[
        intake_object_name(accepted.submission_id, "build_manifest.json")
    ]

    accepted.run()

    assert accepted.record().status == "rejected"
    assert accepted.record().rejection == {
        "code": "object_missing",
        "message": "build_manifest.json is no longer stored.",
    }


def test_a_handler_refusal_rejects_the_submission() -> None:
    accepted = accepted_submission()

    def refuse(context: JobContext) -> dict[str, Any]:
        raise SubmissionRejectedError("invalid_diagnostics", "Schema 7 is retired.")

    accepted.run({"calibration-diagnostics": refuse})

    assert accepted.record().status == "rejected"
    assert accepted.record().rejection == {
        "code": "invalid_diagnostics",
        "message": "Schema 7 is retired.",
    }


def test_a_transient_failure_returns_the_job_for_another_attempt() -> None:
    accepted = accepted_submission()
    diagnostics_object = intake_object_name(
        accepted.submission_id, "calibration_diagnostics.json"
    )
    accepted.objects.unreadable.add(diagnostics_object)

    first = accepted.run()
    accepted.objects.unreadable.clear()
    second = accepted.run()

    assert first is JobOutcome.RETRY
    assert second is JobOutcome.FINISHED
    assert accepted.record().status == "succeeded"
    assert accepted.record().job_attempts == 2


def test_the_last_failed_attempt_fails_the_submission() -> None:
    accepted = accepted_submission()

    def always_fail(context: JobContext) -> dict[str, Any]:
        raise RuntimeError("Tree builder crashed.")

    outcomes = [
        accepted.run({"calibration-diagnostics": always_fail})
        for _ in range(MAX_JOB_ATTEMPTS)
    ]

    assert outcomes == [JobOutcome.RETRY] * (MAX_JOB_ATTEMPTS - 1) + [
        JobOutcome.FINISHED
    ]
    assert accepted.record().status == "failed"
    assert accepted.record().rejection == {
        "code": "worker_error",
        "message": "RuntimeError: Tree builder crashed.",
    }


def test_a_kind_without_a_handler_fails_after_its_attempts() -> None:
    accepted = accepted_submission()

    for _ in range(MAX_JOB_ATTEMPTS):
        accepted.run({})

    assert accepted.record().status == "failed"
    assert accepted.record().rejection is not None
    assert "calibration-diagnostics" in accepted.record().rejection["message"]


def test_a_job_held_by_another_execution_is_skipped() -> None:
    accepted = accepted_submission()
    accepted.submissions.claim_job(accepted.job_id, holder="exec-1", lease_seconds=3600)

    assert accepted.run(holder="exec-2") is JobOutcome.SKIPPED
    assert accepted.record().status == "processing"


def test_a_finished_job_delivered_again_is_skipped() -> None:
    accepted = accepted_submission()
    accepted.run()

    assert accepted.run(holder="exec-2") is JobOutcome.SKIPPED
    assert accepted.record().job_attempts == 1


@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [(JobOutcome.FINISHED, 0), (JobOutcome.SKIPPED, 0), (JobOutcome.RETRY, 1)],
)
def test_only_a_retry_exits_nonzero_so_cloud_run_repeats_the_task(
    outcome: JobOutcome, expected_code: int
) -> None:
    assert exit_code(outcome) == expected_code


def test_the_worker_requires_its_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLLECTOR_WORKER_JOB_ID", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none")
    monkeypatch.setenv("COLLECTOR_INTAKE_BUCKET", "collector-intake")

    with pytest.raises(RuntimeError, match="COLLECTOR_WORKER_JOB_ID"):
        worker.main()


def test_the_worker_runs_the_named_job_as_its_cloud_run_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class StandInReader:
        def __init__(self, **settings: Any) -> None:
            calls["reader"] = settings

    def recorded_run_job(job_id: str, **arguments: Any) -> JobOutcome:
        calls["job_id"] = job_id
        calls["holder"] = arguments["holder"]
        return JobOutcome.RETRY

    monkeypatch.setattr(worker, "GoogleCloudStorageIntake", StandInReader)
    monkeypatch.setattr(worker, "run_job", recorded_run_job)
    monkeypatch.setenv("COLLECTOR_WORKER_JOB_ID", "job-0123")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "collector-worker-abc12")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none")
    monkeypatch.setenv("COLLECTOR_INTAKE_BUCKET", "collector-intake")

    assert worker.main() == 1
    assert calls == {
        "reader": {"bucket_name": "collector-intake"},
        "job_id": "job-0123",
        "holder": "collector-worker-abc12",
    }
