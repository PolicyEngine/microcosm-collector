"""Operator commands: recovery, reprocessing and backfilling calibration trees."""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import httpx
import pytest

from telemetry_collector import calibration_trees, operations
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.calibration_tree_reconciliation import LatestStep
from telemetry_collector.calibration_trees import FollowedLatest
from telemetry_collector.huggingface_read import (
    GENERIC_TOKEN_VARIABLES,
    HuggingFaceReader,
)
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


# Backfilling calibration trees -------------------------------------------------

BACKFILL_MODULE = "telemetry_collector.calibration_tree_backfill"
STORE_BACKFILL_MODULE = "telemetry_collector.calibration_tree_store_backfill"
SOURCE_COPIES_MODULE = "telemetry_collector.calibration_tree_source_copies"
BACKFILL_COMMAND = "backfill-calibration-trees"
PRODUCTION_URL = "https://dashboard.example.test/api/microcosm/tree-manifest"
BACKFILL_ENVIRONMENT = {
    # Never connected to: the tests stand in for the database engine.
    "DATABASE_URL": "postgresql://user:password@database.example.test:1/none",
    "COLLECTOR_HF_READ_TOKEN": "hf_collector_read_token",
    "COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN": "vercel_blob_rw_test_token",
}
LATEST_BUILD = "a" * 64


def backfill_module() -> ModuleType:
    if importlib.util.find_spec(BACKFILL_MODULE) is None:
        pytest.fail(f"{BACKFILL_MODULE} does not exist yet.")
    return importlib.import_module(BACKFILL_MODULE)


def store_backfill_module() -> ModuleType:
    if importlib.util.find_spec(STORE_BACKFILL_MODULE) is None:
        pytest.fail(f"{STORE_BACKFILL_MODULE} does not exist yet.")
    return importlib.import_module(STORE_BACKFILL_MODULE)


def source_files_outcome(**fields: Any) -> Any:
    if importlib.util.find_spec(SOURCE_COPIES_MODULE) is None:
        pytest.fail(f"{SOURCE_COPIES_MODULE} does not exist yet.")
    return importlib.import_module(SOURCE_COPIES_MODULE).SourceFilesOutcome(**fields)


def stand_in_backfill(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]
) -> dict[str, Any]:
    """Replace the backfill's steps with ones that yield ``outcomes`` and record
    their options. A dictionary in ``outcomes`` is an outcome of production's
    manifest; anything else is yielded as it is."""

    module = store_backfill_module()
    reconciliation = importlib.import_module(
        "telemetry_collector.calibration_tree_reconciliation"
    )
    given: dict[str, Any] = {}

    class StandInEngine:
        def dispose(self) -> None:
            given["disposed"] = True

    def recorded_backfill(**options: Any) -> Iterator[Any]:
        given.update(options)
        for outcome in outcomes:
            yield (
                backfill_module().BackfillOutcome(**outcome)
                if isinstance(outcome, dict)
                else outcome
            )

    monkeypatch.setattr(module, "backfill_calibration_tree_store", recorded_backfill)
    monkeypatch.setattr(
        reconciliation, "PostgresSubmissionsInProgress", lambda engine: "submissions"
    )
    monkeypatch.setattr(
        reconciliation, "postgres_reconciliation_lock", lambda engine: "lock"
    )
    monkeypatch.setattr(
        operations, "create_database_engine", lambda database_url: StandInEngine()
    )
    for name in GENERIC_TOKEN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in BACKFILL_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return given


BUILT_LATEST = {
    "country": "us",
    "kind": "release",
    "source_id": "microcosm-us-2025",
    "outcome": "built",
    "make_latest": True,
    "production_build_artifact_id": LATEST_BUILD,
    "build_artifact_id": LATEST_BUILD,
    "matches_production": True,
    "made_latest": True,
}
ALREADY_STORED = {
    "country": "us",
    "kind": "staging",
    "source_id": "us-calibration-attempt-1",
    "outcome": "already_stored",
}


@pytest.mark.parametrize(
    "address",
    ["http://dashboard.example.test/x", "dashboard.example.test/x"],
    ids=["http", "no-scheme"],
)
def test_a_production_manifest_address_must_be_https(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    address: str,
) -> None:
    for name, value in BACKFILL_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as exited:
        operations.main([BACKFILL_COMMAND, "--production-manifest-url", address])

    assert exited.value.code == 2
    assert "--production-manifest-url" in capsys.readouterr().err


def test_the_backfill_runs_without_a_production_manifest_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_backfill(monkeypatch, [])

    try:
        exit_status = operations.main([BACKFILL_COMMAND])
    except SystemExit:
        pytest.fail("The backfill still requires --production-manifest-url.")

    assert exit_status == 0
    assert given["production_manifest_url"] is None
    assert given["production_client"] is None


def test_the_backfill_refuses_a_country_or_kind_it_does_not_know(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stand_in_backfill(monkeypatch, [])

    with pytest.raises(SystemExit) as unknown_country:
        operations.main([BACKFILL_COMMAND, "--country", "fr"])
    with pytest.raises(SystemExit) as unknown_kind:
        operations.main([BACKFILL_COMMAND, "--kind", "comparison"])

    assert unknown_country.value.code == 2
    assert unknown_kind.value.code == 2
    errors = capsys.readouterr().err
    assert "--country" in errors
    assert "--kind" in errors


def test_the_backfill_passes_its_address_countries_kind_and_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_backfill(monkeypatch, [])

    exit_status = operations.main(
        [
            BACKFILL_COMMAND,
            "--production-manifest-url",
            PRODUCTION_URL,
            "--country",
            "uk",
            "--country",
            "us",
            "--kind",
            "staging",
            "--dry-run",
        ]
    )

    assert exit_status == 0
    assert given["production_manifest_url"] == PRODUCTION_URL
    assert isinstance(given["production_client"], httpx.Client)
    assert list(given["countries"]) == ["uk", "us"]
    assert list(given["kinds"]) == ["staging"]
    assert given["dry_run"] is True
    assert given["disposed"] is True


def test_by_default_the_backfill_covers_every_country_and_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_backfill(monkeypatch, [])

    operations.main([BACKFILL_COMMAND])

    assert list(given["countries"]) == ["us", "uk", "be"]
    assert sorted(given["kinds"]) == ["release", "staging"]
    assert given["dry_run"] is False


def test_the_backfill_gives_its_steps_the_reconcile_command_s_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_backfill(monkeypatch, [])

    operations.main([BACKFILL_COMMAND])

    assert given["submissions"] == "submissions"
    assert given["lock"] == "lock"
    assert isinstance(given["reader"], HuggingFaceReader)
    blob_token = BACKFILL_ENVIRONMENT["COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN"]
    commands = given["commands"]
    assert commands.publication_environment[
        "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN"
    ] == (blob_token)
    assert blob_token not in commands.build_environment.values()


def test_the_backfill_copies_and_follows_releases_json_with_the_job_s_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_backfill(monkeypatch, [])
    copies: list[tuple[str, dict[str, Any]]] = []
    followed: list[tuple[str, dict[str, Any]]] = []

    def recorded_copy(country: str, **options: Any) -> str:
        copies.append((country, options))
        return "copied"

    def recorded_follow(country: str, **options: Any) -> str:
        followed.append((country, options))
        return "followed"

    monkeypatch.setattr(calibration_trees, "copy_releases_record", recorded_copy)
    monkeypatch.setattr(calibration_trees, "follow_releases_record", recorded_follow)

    operations.main([BACKFILL_COMMAND])

    blob_token = BACKFILL_ENVIRONMENT["COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN"]
    assert given["copy_record"]("uk", dry_run=True) == "copied"
    [(country, options)] = copies
    assert country == "uk"
    assert options["dry_run"] is True
    assert options["blob_token"] == blob_token
    assert isinstance(options["reader"], HuggingFaceReader)
    assert given["follow_latest"]("us") == "followed"
    [(country, options)] = followed
    assert country == "us"
    assert options["blob_token"] == blob_token
    assert callable(options["manifest_lock"])


@pytest.mark.parametrize("missing_name", list(BACKFILL_ENVIRONMENT))
def test_the_backfill_names_a_missing_variable(
    monkeypatch: pytest.MonkeyPatch, missing_name: str
) -> None:
    stand_in_backfill(monkeypatch, [])
    monkeypatch.delenv(missing_name)

    with pytest.raises(RuntimeError, match=missing_name):
        operations.main([BACKFILL_COMMAND])


def test_the_backfill_prints_one_document_per_outcome_under_its_step_then_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reconciliation = importlib.import_module(
        "telemetry_collector.calibration_tree_reconciliation"
    )
    record_step = reconciliation.ReleasesRecordStep(
        country="us",
        outcome="stored",
        copied=calibration_trees.CopiedReleasesRecord(
            country="us",
            outcome="stored",
            hf_repo="policyengine/populace-us",
            hf_commit="c" * 40,
            has_record=True,
        ),
    )
    staging_run = reconciliation.ReconcileOutcome(
        country="us", kind="staging", outcome="built", source_id="run-1"
    )
    copied = source_files_outcome(
        country="us",
        outcome="copied",
        kind="release",
        source_id="microcosm-us-2025",
        files={"reform_validation.json": "copied"},
    )
    stand_in_backfill(
        monkeypatch, [record_step, BUILT_LATEST, ALREADY_STORED, staging_run, copied]
    )

    exit_status = operations.main(
        [BACKFILL_COMMAND, "--production-manifest-url", PRODUCTION_URL]
    )

    assert exit_status == 0
    printed = [json.loads(text) for text in capsys.readouterr().out.splitlines()]
    assert [list(document) for document in printed] == [
        ["releases_record"],
        ["production_build"],
        ["production_build"],
        ["staging_run"],
        ["source_files"],
        ["summary"],
    ]
    assert printed[1]["production_build"]["outcome"] == "built"
    assert printed[4]["source_files"]["files"] == {"reform_validation.json": "copied"}
    summary = printed[-1]["summary"]
    assert summary["incomplete"] is False
    assert summary["dry_run"] is False
    assert summary["releases_records"]["us"]["outcome"] == "stored"
    assert summary["production_builds"] == {
        "outcomes": {"built": 1, "already_stored": 1},
        "incomplete": False,
        "release_builds_differing_from_production": 0,
        "latest": {
            "us": {"build": LATEST_BUILD, "made_latest": True},
            "uk": {"build": None, "made_latest": None},
            "be": {"build": None, "made_latest": None},
        },
    }
    assert summary["staging_runs"]["us"]["outcomes"] == {"built": 1}
    assert summary["source_files"]["outcomes"] == {"copied": 1}


def test_without_a_production_manifest_address_the_summary_has_no_production_part(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stand_in_backfill(monkeypatch, [])

    operations.main([BACKFILL_COMMAND])

    summary = json.loads(capsys.readouterr().out.splitlines()[-1])["summary"]
    assert summary["production_builds"] is None


@pytest.mark.parametrize(
    "incomplete_outcome",
    [
        {**ALREADY_STORED, "outcome": "error", "reason": "boom", "incomplete": True},
        {**ALREADY_STORED, "outcome": "deferred", "incomplete": True},
        {"country": "uk", "outcome": "error", "reason": "HTTP 500", "incomplete": True},
        {**BUILT_LATEST, "outcome": "latest_not_moved", "made_latest": False},
        {**BUILT_LATEST, "outcome": "invalid", "made_latest": False},
        "staging-run-error",
        "source-files-deferred",
        "releases-record-error",
    ],
    ids=[
        "error",
        "deferred",
        "country-failed",
        "latest-not-moved",
        "latest-invalid",
        "staging-run-error",
        "source-files-deferred",
        "releases-record-error",
    ],
)
def test_the_backfill_exits_1_when_anything_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    incomplete_outcome: Any,
) -> None:
    reconciliation = importlib.import_module(
        "telemetry_collector.calibration_tree_reconciliation"
    )
    if incomplete_outcome == "staging-run-error":
        incomplete_outcome = reconciliation.ReconcileOutcome(
            country="uk", kind="staging", outcome="error", incomplete=True
        )
    elif incomplete_outcome == "source-files-deferred":
        incomplete_outcome = source_files_outcome(
            country="us", outcome="deferred", kind="release", incomplete=True
        )
    elif incomplete_outcome == "releases-record-error":
        incomplete_outcome = reconciliation.ReleasesRecordStep(
            country="be", outcome="error", reason="boom", incomplete=True
        )
    stand_in_backfill(monkeypatch, [incomplete_outcome])

    exit_status = operations.main(
        [BACKFILL_COMMAND, "--production-manifest-url", PRODUCTION_URL]
    )

    assert exit_status == 1
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])["summary"]
    assert summary["incomplete"] is True


def test_final_refusals_alone_do_not_fail_the_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stand_in_backfill(
        monkeypatch,
        [
            {**ALREADY_STORED, "outcome": outcome, "source_id": f"run-{outcome}"}
            for outcome in (
                "unbuildable",
                "refused",
                "too_large",
                "invalid",
                "mismatch",
            )
        ]
        + [
            source_files_outcome(country="us", outcome=outcome, kind="release")
            for outcome in ("not_copied", "not_found", "invalid")
        ],
    )

    exit_status = operations.main(
        [BACKFILL_COMMAND, "--production-manifest-url", PRODUCTION_URL]
    )

    assert exit_status == 0


# Reconciling calibration trees ------------------------------------------------

RECONCILIATION_MODULE = "telemetry_collector.calibration_tree_reconciliation"
BUILDS_MODULE = "telemetry_collector.calibration_tree_builds"
RECONCILE_COMMAND = "reconcile-calibration-trees"
RECONCILE_ENVIRONMENT = {
    # Never connected to: the tests stand in for the database engine.
    "DATABASE_URL": "postgresql://user:password@database.example.test:1/none",
    "COLLECTOR_HF_READ_TOKEN": "hf_collector_read_token",
    "COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN": "vercel_blob_rw_test_token",
}
REQUEST_ID = "calibration-trees-uk-staging-2960000"
EXECUTION = "collector-worker-x7k2p"


def imported_module(name: str) -> ModuleType:
    if importlib.util.find_spec(name) is None:
        pytest.fail(f"{name} does not exist yet.")
    return importlib.import_module(name)


class StandInRequestRows:
    """Stands in for the calibration_tree_build_requests table."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def record_start(self, task_id: str, **values: Any) -> bool:
        self.calls.append(("start", task_id, values))
        return True

    def record_finish(self, task_id: str, **values: Any) -> bool:
        self.calls.append(("finish", task_id, values))
        return True


def stand_in_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[Any],
    *,
    raised: Exception | None = None,
) -> dict[str, Any]:
    """Replace the reconciliation with one that yields ``outcomes`` (then
    raises ``raised``, if given) and records its options. A dictionary in
    ``outcomes`` is a build's outcome; anything else is yielded as it is."""

    reconciliation = imported_module(RECONCILIATION_MODULE)
    builds = imported_module(BUILDS_MODULE)
    given: dict[str, Any] = {"request_rows": []}

    class StandInEngine:
        def dispose(self) -> None:
            given["disposed"] = True

    def recorded_reconciliation(**options: Any) -> Iterator[Any]:
        given.update(options)
        for outcome in outcomes:
            yield (
                reconciliation.ReconcileOutcome(**outcome)
                if isinstance(outcome, dict)
                else outcome
            )
        if raised is not None:
            raise raised

    def request_rows(engine: Any) -> StandInRequestRows:
        rows = StandInRequestRows(engine)
        given["request_rows"].append(rows)
        return rows

    monkeypatch.setattr(
        reconciliation, "reconcile_calibration_trees", recorded_reconciliation
    )
    monkeypatch.setattr(
        reconciliation, "PostgresSubmissionsInProgress", lambda engine: "submissions"
    )
    monkeypatch.setattr(
        reconciliation, "postgres_reconciliation_lock", lambda engine: "lock"
    )
    monkeypatch.setattr(builds, "PostgresCalibrationTreeBuildRequests", request_rows)
    monkeypatch.setattr(
        operations, "create_database_engine", lambda database_url: StandInEngine()
    )
    for name in GENERIC_TOKEN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in RECONCILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", EXECUTION)
    return given


BUILT_RUN = {
    "country": "uk",
    "kind": "staging",
    "outcome": "built",
    "source_id": "uk-frs-calibration-attempt-1",
    "build_artifact_id": "c" * 64,
}
STORED_RUN = {
    "country": "uk",
    "kind": "staging",
    "outcome": "already_stored",
    "source_id": "uk-frs-calibration-attempt-0",
}
US_CURRENT_BUILD = "microcosm-us-2025/us-calibration-attempt-1"
BUILT_RELEASE = {
    "country": "us",
    "kind": "release",
    "outcome": "built",
    "source_id": US_CURRENT_BUILD,
    "build_artifact_id": "d" * 64,
    "current_default": True,
}


def latest_step(**fields: Any) -> LatestStep:
    """A US release run's latest step."""

    return LatestStep(country="us", **fields)


def followed_latest() -> FollowedLatest:
    return FollowedLatest(
        country="us",
        current_build=US_CURRENT_BUILD,
        build_artifact_id="d" * 64,
        made_latest=True,
        latest_moved=True,
    )


def test_the_reconcile_command_passes_its_country_kind_and_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_reconciliation(monkeypatch, [])

    exit_status = operations.main(
        [RECONCILE_COMMAND, "--country", "uk", "--kind", "staging", "--dry-run"]
    )

    assert exit_status == 0
    assert given["country"] == "uk"
    assert list(given["kinds"]) == ["staging"]
    assert given["dry_run"] is True
    assert given["submissions"] == "submissions"
    assert given["lock"] == "lock"
    assert given["disposed"] is True


def test_the_reconcile_command_leaves_older_runs_out_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_reconciliation(monkeypatch, [])

    operations.main([RECONCILE_COMMAND, "--country", "uk", "--kind", "staging"])

    assert given.get("include_older_runs") is False


def test_the_reconcile_command_can_include_older_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_reconciliation(monkeypatch, [])

    try:
        operations.main(
            [
                RECONCILE_COMMAND,
                "--country",
                "uk",
                "--kind",
                "staging",
                "--include-older-runs",
            ]
        )
    except SystemExit:
        pytest.fail("The command has no --include-older-runs yet.")

    assert given.get("include_older_runs") is True


@pytest.mark.parametrize(
    ("country", "kinds"),
    [
        ("us", ["release", "staging"]),
        ("uk", ["release", "staging"]),
        ("be", ["release"]),
    ],
)
def test_without_a_kind_the_command_reconciles_every_kind_the_country_has(
    monkeypatch: pytest.MonkeyPatch, country: str, kinds: list[str]
) -> None:
    given = stand_in_reconciliation(monkeypatch, [])

    operations.main([RECONCILE_COMMAND, "--country", country])

    assert list(given["kinds"]) == kinds
    assert given["dry_run"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--country", "fr"],
        ["--country", "uk", "--kind", "comparison"],
        ["--country", "be", "--kind", "staging"],
        ["--country", "uk", "--kind", "staging", "--request-id", "anything"],
        [
            "--country",
            "uk",
            "--kind",
            "release",
            "--request-id",
            "calibration-trees-uk-staging-2960000",
        ],
        [
            "--country",
            "us",
            "--kind",
            "staging",
            "--request-id",
            "calibration-trees-uk-staging-2960000",
        ],
        ["--country", "uk", "--request-id", REQUEST_ID],
        [
            "--country",
            "uk",
            "--kind",
            "staging",
            "--request-id",
            REQUEST_ID,
            "--dry-run",
        ],
        [
            "--country",
            "uk",
            "--kind",
            "staging",
            "--request-id",
            REQUEST_ID,
            "--include-older-runs",
        ],
    ],
    ids=[
        "no-country",
        "unknown-country",
        "unknown-kind",
        "belgian-staging",
        "malformed-request-id",
        "request-id-of-another-kind",
        "request-id-of-another-country",
        "request-id-without-kind",
        "request-id-in-a-dry-run",
        "request-id-with-older-runs",
    ],
)
def test_the_reconcile_command_exits_2_for_an_argument_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    given = stand_in_reconciliation(monkeypatch, [])

    with pytest.raises(SystemExit) as exited:
        operations.main([RECONCILE_COMMAND, *arguments])

    assert exited.value.code == 2
    assert capsys.readouterr().err
    assert "country" not in given


def test_the_reconcile_command_prints_one_document_per_entry_then_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stand_in_reconciliation(monkeypatch, [BUILT_RUN, STORED_RUN])

    exit_status = operations.main(
        [RECONCILE_COMMAND, "--country", "uk", "--kind", "staging"]
    )

    assert exit_status == 0
    printed = [json.loads(text) for text in capsys.readouterr().out.splitlines()]
    assert len(printed) == 3
    assert [
        (document["source_id"], document["outcome"]) for document in printed[:2]
    ] == [
        ("uk-frs-calibration-attempt-1", "built"),
        ("uk-frs-calibration-attempt-0", "already_stored"),
    ]
    assert printed[2] == {
        "summary": {
            "country": "uk",
            "kinds": ["staging"],
            "request_id": None,
            "dry_run": False,
            "outcomes": {"built": 1, "already_stored": 1},
            "incomplete": False,
            "incomplete_sources": [],
            "latest": None,
            "releases_record": None,
        }
    }


@pytest.mark.parametrize(
    "incomplete_outcome",
    [
        {**BUILT_RUN, "outcome": "error", "reason": "boom", "incomplete": True},
        {**BUILT_RUN, "outcome": "deferred", "incomplete": True},
        {
            "country": "uk",
            "kind": "staging",
            "outcome": "lock_wait_expired",
            "incomplete": True,
        },
    ],
    ids=["error", "deferred", "lock-wait-expired"],
)
def test_the_reconcile_command_exits_1_when_the_run_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    incomplete_outcome: dict[str, Any],
) -> None:
    stand_in_reconciliation(monkeypatch, [STORED_RUN, incomplete_outcome])

    exit_status = operations.main(
        [RECONCILE_COMMAND, "--country", "uk", "--kind", "staging"]
    )

    assert exit_status == 1
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])["summary"]
    assert summary["incomplete"] is True


@pytest.mark.parametrize(
    "final_outcome",
    [
        "ineligible",
        "unbuildable",
        "refused",
        "too_large",
        "submission_in_progress",
        "no_releases_record",
        "releases_record_refused",
    ],
)
def test_final_outcomes_alone_do_not_fail_the_reconcile_command(
    monkeypatch: pytest.MonkeyPatch, final_outcome: str
) -> None:
    stand_in_reconciliation(monkeypatch, [{**STORED_RUN, "outcome": final_outcome}])

    assert operations.main([RECONCILE_COMMAND, "--country", "uk"]) == 0


def test_a_release_run_prints_its_latest_step_and_its_summary_reports_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    step = latest_step(outcome="followed", followed=followed_latest())
    stand_in_reconciliation(monkeypatch, [BUILT_RELEASE, step])

    exit_status = operations.main(
        [RECONCILE_COMMAND, "--country", "us", "--kind", "release"]
    )

    assert exit_status == 0
    printed = [json.loads(text) for text in capsys.readouterr().out.splitlines()]
    assert len(printed) == 3
    expected_latest = {
        "country": "us",
        "outcome": "followed",
        "current_build": US_CURRENT_BUILD,
        "build_artifact_id": "d" * 64,
        "made_latest": True,
        "latest_moved": True,
        "incomplete": False,
    }
    assert printed[1] == {"latest": expected_latest}
    summary = printed[2]["summary"]
    assert summary["latest"] == expected_latest
    assert summary["outcomes"] == {"built": 1}
    assert summary["incomplete"] is False


def test_a_latest_step_that_failed_makes_the_command_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    step = latest_step(
        outcome="error",
        reason="CalibrationTreeCommandError: The tree latest command exited with 3.",
        incomplete=True,
    )
    stand_in_reconciliation(monkeypatch, [BUILT_RELEASE, step])

    exit_status = operations.main(
        [RECONCILE_COMMAND, "--country", "us", "--kind", "release"]
    )

    assert exit_status == 1
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])["summary"]
    assert summary["incomplete"] is True
    assert summary["latest"]["outcome"] == "error"


def releases_record_step(**fields: Any) -> Any:
    """A US release run's step that copies releases.json."""

    reconciliation = imported_module(RECONCILIATION_MODULE)
    assert hasattr(reconciliation, "ReleasesRecordStep")
    return reconciliation.ReleasesRecordStep(country="us", **fields)


def test_a_release_run_prints_its_copy_step_and_its_summary_reports_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hasattr(calibration_trees, "CopiedReleasesRecord")
    step = releases_record_step(
        outcome="stored",
        copied=calibration_trees.CopiedReleasesRecord(
            country="us",
            outcome="stored",
            hf_repo="policyengine/populace-us",
            hf_commit="c" * 40,
            has_record=True,
        ),
    )
    stand_in_reconciliation(monkeypatch, [step, BUILT_RELEASE])

    exit_status = operations.main(
        [RECONCILE_COMMAND, "--country", "us", "--kind", "release"]
    )

    assert exit_status == 0
    printed = [json.loads(text) for text in capsys.readouterr().out.splitlines()]
    expected_step = {
        "country": "us",
        "outcome": "stored",
        "hf_repo": "policyengine/populace-us",
        "hf_commit": "c" * 40,
        "has_record": True,
        "incomplete": False,
    }
    assert printed[0] == {"releases_record": expected_step}
    assert printed[-1]["summary"]["releases_record"] == expected_step
    assert printed[-1]["summary"]["outcomes"] == {"built": 1}


def test_a_copy_step_that_failed_makes_the_command_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    step = releases_record_step(outcome="error", reason="boom", incomplete=True)
    stand_in_reconciliation(monkeypatch, [step, BUILT_RELEASE])

    exit_status = operations.main(
        [RECONCILE_COMMAND, "--country", "us", "--kind", "release"]
    )

    assert exit_status == 1
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])["summary"]
    assert summary["incomplete"] is True
    assert summary["releases_record"]["outcome"] == "error"


def test_the_reconcile_command_copies_releases_json_with_the_worker_job_s_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_reconciliation(monkeypatch, [])
    copies: list[tuple[str, dict[str, Any]]] = []

    def recorded_copy(country: str, **options: Any) -> str:
        copies.append((country, options))
        return "copied"

    monkeypatch.setattr(
        calibration_trees, "copy_releases_record", recorded_copy, raising=False
    )

    operations.main([RECONCILE_COMMAND, "--country", "us", "--kind", "release"])

    copy_record = given.get("copy_record")
    assert callable(copy_record), "The command passes no copy_record."
    assert copy_record("us") == "copied"
    [(country, options)] = copies
    assert country == "us"
    assert (
        options["blob_token"]
        == (RECONCILE_ENVIRONMENT["COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN"])
    )
    assert isinstance(options["reader"], HuggingFaceReader)


def test_the_reconcile_command_follows_releases_json_with_the_worker_job_s_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_reconciliation(monkeypatch, [])
    followed: list[tuple[str, dict[str, Any]]] = []

    def recorded_follow(country: str, **options: Any) -> FollowedLatest:
        followed.append((country, options))
        return followed_latest()

    monkeypatch.setattr(calibration_trees, "follow_releases_record", recorded_follow)

    operations.main([RECONCILE_COMMAND, "--country", "us", "--kind", "release"])

    follow_latest = given.get("follow_latest")
    assert callable(follow_latest), "The command passes no follow_latest."
    assert follow_latest("us") == followed_latest()
    [(country, options)] = followed
    blob_token = RECONCILE_ENVIRONMENT["COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN"]
    assert country == "us"
    assert options["blob_token"] == blob_token
    assert callable(options["manifest_lock"])
    assert isinstance(options["reader"], HuggingFaceReader)


def test_a_queued_run_records_its_start_and_finish_on_its_request_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    given = stand_in_reconciliation(monkeypatch, [BUILT_RUN])

    exit_status = operations.main(
        [
            RECONCILE_COMMAND,
            "--country",
            "uk",
            "--kind",
            "staging",
            "--request-id",
            REQUEST_ID,
        ]
    )

    assert exit_status == 0
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])["summary"]
    assert summary["request_id"] == REQUEST_ID
    [rows] = given["request_rows"]
    [(start, start_id, started), (finish, finish_id, finished)] = rows.calls
    assert (start, start_id) == ("start", REQUEST_ID)
    assert started["execution"] == EXECUTION
    assert (finish, finish_id) == ("finish", REQUEST_ID)
    assert finished["summary"] == summary
    assert finished["at"] >= started["at"]


def test_an_operator_s_run_without_a_request_id_records_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_reconciliation(monkeypatch, [BUILT_RUN])

    operations.main([RECONCILE_COMMAND, "--country", "uk", "--kind", "staging"])

    assert all(rows.calls == [] for rows in given["request_rows"])


def test_a_run_that_stops_on_an_unexpected_error_still_records_its_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = stand_in_reconciliation(
        monkeypatch, [BUILT_RUN], raised=RuntimeError("unexpected")
    )

    with pytest.raises(RuntimeError, match="unexpected"):
        operations.main(
            [
                RECONCILE_COMMAND,
                "--country",
                "uk",
                "--kind",
                "staging",
                "--request-id",
                REQUEST_ID,
            ]
        )

    [rows] = given["request_rows"]
    assert [call[0] for call in rows.calls] == ["start", "finish"]
    summary = rows.calls[1][2]["summary"]
    assert summary["incomplete"] is True
    assert summary["stopped_by"] == "RuntimeError"


@pytest.mark.parametrize("missing_name", list(RECONCILE_ENVIRONMENT))
def test_the_reconcile_command_names_a_missing_variable(
    monkeypatch: pytest.MonkeyPatch, missing_name: str
) -> None:
    given = stand_in_reconciliation(monkeypatch, [])
    monkeypatch.delenv(missing_name)

    with pytest.raises(RuntimeError, match=missing_name):
        operations.main(
            [
                RECONCILE_COMMAND,
                "--country",
                "uk",
                "--kind",
                "staging",
                "--request-id",
                REQUEST_ID,
            ]
        )
    # Nothing is recorded for a run that could not start.
    assert all(rows.calls == [] for rows in given["request_rows"])
