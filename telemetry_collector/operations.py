"""Operator commands for artifact jobs.

``recover`` runs on a schedule (every 10 minutes). It finds jobs whose attempt
or execution ended without recording a result, such as a memory kill, a task
timeout or an execution that exited before claiming its job, and either fails
them, when they used their attempts or dispatches, or dispatches them again.

``reprocess`` records the next processing round for final submissions and
dispatches it: after the calibration-tree or Orrery worker registers its
handler, or when the projection or Orrery changes.

``past-builds`` prints the inventory of past Microcosm builds
(``telemetry_collector.past_builds``) as JSON documents, the inventory's
totals first and then one for each build, and changes nothing. It reads
Hugging Face, so it runs as an execution of the worker job, the only job with
``COLLECTOR_HF_READ_TOKEN``.

``reproject-past-builds`` makes an ``orrery-inputs`` submission for each past
build that can be projected again and processes it at once
(``telemetry_collector.past_build_projections``). Besides the Hugging Face
token it needs the worker job's intake and Orrery buckets, and permission for
the worker's account to create objects in the intake bucket. It prints each
build's outcome as it is produced, then a summary of the run, and exits
non-zero when any build is incomplete: it ended in an error, or GitHub stopped
answering before it was classed. With ``--dry-run`` it only lists what it would
project, from Hugging Face's listings: it downloads no build's graph inputs
and writes nothing.

Run them with ``python -m telemetry_collector.operations recover``,
``python -m telemetry_collector.operations reprocess``,
``python -m telemetry_collector.operations past-builds`` and
``python -m telemetry_collector.operations reproject-past-builds``.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import Any, get_args

from telemetry_collector import past_builds
from telemetry_collector.database import create_database_engine
from telemetry_collector.google_cloud import (
    CloudTasksWorkerQueue,
    GoogleCloudStorageIntake,
)
from telemetry_collector.huggingface_read import HuggingFaceReader
from telemetry_collector.intake import (
    BUCKET_PATTERN,
    JOB_PATTERN,
    QUEUE_PATTERN,
    SERVICE_ACCOUNT_PATTERN,
    WorkerQueue,
    WorkerQueueUnavailableError,
    dispatch_job,
    resource_setting,
)
from telemetry_collector.submission_store import (
    JobRecovery,
    PostgresSubmissionStore,
    SubmissionNotReprocessableError,
    SubmissionStore,
)
from telemetry_collector.submissions import (
    UNCLAIMED_JOB_IDLE_SECONDS,
    SubmissionKind,
)

OPERATIONS_REQUIREMENT = "running operator commands"
REPROJECTION_REQUIREMENT = "projecting past builds again"


def recover_stalled_jobs(
    submissions: SubmissionStore,
    worker_queue: WorkerQueue,
    *,
    idle_seconds: float = UNCLAIMED_JOB_IDLE_SECONDS,
) -> list[JobRecovery]:
    """Fail or dispatch again every stalled job; safe to run concurrently."""

    recoveries = []
    for job_id in submissions.stalled_job_ids(idle_seconds=idle_seconds):
        recovery = submissions.recover_job(job_id, idle_seconds=idle_seconds)
        if recovery.action == "dispatch":
            assert recovery.dispatch_number is not None
            try:
                dispatch_job(
                    submissions, worker_queue, job_id, recovery.dispatch_number
                )
            except WorkerQueueUnavailableError:
                # The job stays queued with its dispatch count unchanged, so
                # a later recovery repeats this dispatch under the same name.
                recovery = replace(recovery, action="dispatch_failed")
        if recovery.action != "none":
            recoveries.append(recovery)
    return recoveries


def reprocess_submissions(
    submissions: SubmissionStore,
    worker_queue: WorkerQueue,
    submission_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Record and dispatch the next processing round of each submission."""

    outcomes: list[dict[str, Any]] = []
    for submission_id in submission_ids:
        try:
            record = submissions.reprocess_submission(submission_id)
        except KeyError:
            outcomes.append({"submission_id": submission_id, "outcome": "not_found"})
            continue
        except SubmissionNotReprocessableError as error:
            outcomes.append(
                {
                    "submission_id": submission_id,
                    "outcome": "refused",
                    "reason": str(error),
                }
            )
            continue
        assert record.job_id is not None
        outcome: dict[str, Any] = {
            "submission_id": submission_id,
            "outcome": "reprocessing",
            "job_id": record.job_id,
            "processing_round": record.processing_round,
        }
        try:
            dispatch_job(submissions, worker_queue, record.job_id, 1)
        except WorkerQueueUnavailableError:
            # Recovery dispatches the queued job once it has been idle.
            outcome["outcome"] = "reprocessing_not_dispatched"
        outcomes.append(outcome)
    return outcomes


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m telemetry_collector.operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("recover", help="Fail or dispatch again every stalled job.")
    reprocess = commands.add_parser(
        "reprocess", help="Process final submissions again in a new round."
    )
    reprocess.add_argument("submission_ids", nargs="*", metavar="SUBMISSION_ID")
    reprocess.add_argument(
        "--kind",
        choices=get_args(SubmissionKind),
        help="Select every final submission of this kind.",
    )
    reprocess.add_argument(
        "--intake-only",
        action="store_true",
        help="With --kind, select only submissions that had only their bytes checked.",
    )
    reprocess.add_argument(
        "--dry-run",
        action="store_true",
        help="List the selected submissions without reprocessing them.",
    )
    commands.add_parser(
        "past-builds",
        help="Print the inventory of past Microcosm builds as JSON.",
    )
    reproject = commands.add_parser(
        "reproject-past-builds",
        help="Publish Orrery graph files for past builds from their staged inputs.",
    )
    reproject.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "List what would be projected, downloading no graph inputs and "
            "writing nothing."
        ),
    )
    return parser


def _worker_queue_from_environment() -> CloudTasksWorkerQueue:
    return CloudTasksWorkerQueue(
        queue=resource_setting(
            "COLLECTOR_WORKER_QUEUE", QUEUE_PATTERN, required_by=OPERATIONS_REQUIREMENT
        ),
        worker_job=resource_setting(
            "COLLECTOR_WORKER_JOB", JOB_PATTERN, required_by=OPERATIONS_REQUIREMENT
        ),
        invoker_service_account=resource_setting(
            "COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT",
            SERVICE_ACCOUNT_PATTERN,
            required_by=OPERATIONS_REQUIREMENT,
        ),
    )


def _print_json(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True), flush=True)


def _past_build_inventory(
    reader: HuggingFaceReader, engine: Any
) -> past_builds.PastBuildInventory:
    runs = past_builds.recorded_runs(engine)
    with past_builds.GitHubCommitClassifier() as classifier:
        return past_builds.list_past_builds(reader, runs, classifier)


def _print_past_builds(database_url: str) -> int:
    with HuggingFaceReader.from_environment() as reader:
        engine = create_database_engine(database_url)
        try:
            inventory = _past_build_inventory(reader, engine)
        finally:
            engine.dispose()
    # One document for the inventory and then one for each build, so that no
    # log entry grows with the number of builds.
    document = inventory.to_json()
    builds = document.pop("builds")
    _print_json(document)
    for build in builds:
        _print_json(build)
    return 0


def _reproject_past_builds(database_url: str, *, dry_run: bool) -> int:
    # Imported here: the handlers, and the projections that run them, build
    # on the worker, which recovery and reprocessing don't need.
    from telemetry_collector import orrery_graphs, past_build_projections
    from telemetry_collector.handlers import handlers_from_environment

    intake_bucket = None
    if not dry_run:
        intake_bucket = resource_setting(
            "COLLECTOR_INTAKE_BUCKET",
            BUCKET_PATTERN,
            required_by=REPROJECTION_REQUIREMENT,
        )
        if orrery_graphs.orrery_bucket() is None:
            raise RuntimeError(
                f"{orrery_graphs.BUCKET_SETTING} is required when "
                f"{REPROJECTION_REQUIREMENT}."
            )
    holder = f"reprojection-{os.environ.get('CLOUD_RUN_EXECUTION') or uuid.uuid4().hex}"
    outcome_counts: Counter[str] = Counter()
    incomplete_run_ids: list[str] = []
    with HuggingFaceReader.from_environment() as reader:
        engine = create_database_engine(database_url)
        try:
            inventory = _past_build_inventory(reader, engine)
            outcomes = past_build_projections.reproject_past_builds(
                inventory,
                reader,
                submissions=PostgresSubmissionStore(engine),
                objects=(
                    None
                    if intake_bucket is None
                    else GoogleCloudStorageIntake(bucket_name=intake_bucket)
                ),
                handlers={} if dry_run else handlers_from_environment(engine=engine),
                holder=holder,
                dry_run=dry_run,
            )
            # Each outcome is printed as it is produced, so a run that stops
            # early still logs every build it finished.
            for outcome in outcomes:
                _print_json(asdict(outcome))
                outcome_counts[outcome.outcome] += 1
                if outcome.incomplete:
                    incomplete_run_ids.append(outcome.run_id)
        finally:
            engine.dispose()
    # The summary says whether the run is complete; a task retry starts the
    # inventory again, so only a summary without incomplete builds says so.
    _print_json(
        {
            "summary": {
                "outcomes": dict(outcome_counts),
                "incomplete_run_ids": incomplete_run_ids,
            }
        }
    )
    return 1 if incomplete_run_ids else 0


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one operator command; non-zero when a reprocessing was refused."""

    options = _argument_parser().parse_args(arguments)
    if options.command == "reprocess" and bool(options.submission_ids) == bool(
        options.kind
    ):
        raise SystemExit("Name submission IDs or --kind, not both and not neither.")
    if options.command == "reprocess" and options.intake_only and not options.kind:
        raise SystemExit("--intake-only needs --kind.")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError(f"DATABASE_URL is required when {OPERATIONS_REQUIREMENT}.")
    if options.command == "past-builds":
        return _print_past_builds(database_url)
    if options.command == "reproject-past-builds":
        return _reproject_past_builds(database_url, dry_run=options.dry_run)
    dry_run = options.command == "reprocess" and options.dry_run
    worker_queue = None if dry_run else _worker_queue_from_environment()
    engine = create_database_engine(database_url)
    try:
        submissions = PostgresSubmissionStore(engine)
        if options.command == "recover":
            assert worker_queue is not None
            for recovery in recover_stalled_jobs(submissions, worker_queue):
                _print_json(
                    {
                        "job_id": recovery.job_id,
                        "action": recovery.action,
                        "dispatch_number": recovery.dispatch_number,
                    }
                )
            return 0
        submission_ids = list(options.submission_ids) or (
            submissions.reprocessable_submission_ids(
                options.kind, intake_only=options.intake_only
            )
        )
        if dry_run:
            for submission_id in submission_ids:
                _print_json(
                    {"submission_id": submission_id, "outcome": "would_reprocess"}
                )
            return 0
        assert worker_queue is not None
        outcomes = reprocess_submissions(submissions, worker_queue, submission_ids)
        for outcome in outcomes:
            _print_json(outcome)
        refused = {"not_found", "refused"}
        return 1 if any(outcome["outcome"] in refused for outcome in outcomes) else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
