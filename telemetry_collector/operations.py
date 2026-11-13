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

``backfill-calibration-trees --production-manifest-url URL`` fills the
collector's calibration tree store with every build production's public tree
manifest lists (``telemetry_collector.calibration_tree_backfill``), building
each missing one from its Hugging Face files. ``URL`` is the dashboard's
``https://<dashboard>/api/microcosm/tree-manifest``, which it requests for
each country without any credential. ``--country`` (repeatable) and
``--kind`` limit it, and ``--dry-run`` lists what it would build, from
Hugging Face's listings and the store's manifest, downloading no diagnostics
and building nothing. It needs ``DATABASE_URL`` (for the manifest lock),
``COLLECTOR_HF_READ_TOKEN`` and ``COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN``, so
it runs as an execution of the worker job with its arguments overridden. It
prints one document for each entry, then a summary, and exits 1 when the run
is incomplete: an entry ended in an error or was deferred past the time
limit, a country's manifest could not be used, or a country's latest build
was not made the store's latest; and 2 for an argument error.

``reconcile-calibration-trees --country <cc> [--kind release|staging]
[--request-id <task ID>] [--dry-run]`` builds the country's calibration trees
that its Hugging Face repos hold and the store lacks
(``telemetry_collector.calibration_tree_reconciliation``): every build its
release repo's ``releases.json`` lists, and every finalized run of its staging
repo, each read at the repo's head. Without ``--kind`` it covers both kinds,
or only releases for a country without a staging repo. The Cloud Task that
``POST /v1/calibration-tree-builds`` queues runs it in the worker job with
``--request-id``, the task's ID: the run records its start, attempts, finish
and summary on that request's row of ``calibration_tree_build_requests``.
``--request-id`` must name the same country and kind, and can't be used with
``--dry-run``. It needs ``DATABASE_URL`` (for its locks, the submissions it
skips and the request's row), ``COLLECTOR_HF_READ_TOKEN`` and
``COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN``, which the worker job has. It
prints one document for each build, then a summary, and exits 1 when the run
is incomplete: a build ended in an error or was deferred past the time limit,
a repo or the store's manifest could not be read, or another execution held
the country's and kind's lock past the 15-minute wait; and 2 for an argument
error. Nothing it publishes asks to become the country's latest. Instead, a
release run ends by making the country's latest follow ``releases.json``
(``calibration_trees.follow_releases_record``), which builds nothing: it
prints that step's document, ``{"latest": {...}}``, which the summary's
``latest`` repeats, and exits 1 when the step failed in a way another
attempt may get past. With ``--dry-run`` it lists what it would build, from
the Hugging Face listings and the store's manifest: it downloads no
diagnostics, builds nothing, takes no lock and leaves the latest as it is.

Run them with ``python -m telemetry_collector.operations recover``,
``python -m telemetry_collector.operations reprocess``,
``python -m telemetry_collector.operations past-builds``,
``python -m telemetry_collector.operations reproject-past-builds``,
``python -m telemetry_collector.operations backfill-calibration-trees`` and
``python -m telemetry_collector.operations reconcile-calibration-trees``.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any, get_args
from urllib.parse import urlsplit

import httpx

from telemetry_collector import (
    calibration_tree_backfill,
    calibration_tree_builds,
    calibration_tree_reconciliation,
    calibration_trees,
    past_builds,
)
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
BACKFILL_REQUIREMENT = "backfilling calibration trees"
BACKFILL_COMMAND = "backfill-calibration-trees"
RECONCILE_REQUIREMENT = "reconciling calibration trees"
RECONCILE_COMMAND = calibration_tree_builds.RECONCILE_COMMAND
#: Projected builds whose document isn't published: their jobs wait for the
#: scheduled recovery (queued, processing) or ended without one (failed,
#: rejected). They don't make the command exit non-zero.
UNPUBLISHED_OUTCOMES = frozenset({"queued", "processing", "failed", "rejected"})


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
    backfill = commands.add_parser(
        BACKFILL_COMMAND,
        help="Build every calibration tree production's tree manifest lists.",
    )
    backfill.add_argument(
        "--production-manifest-url",
        required=True,
        metavar="URL",
        help="Production's tree manifest, https://<dashboard>/api/microcosm/tree-manifest.",
    )
    backfill.add_argument(
        "--country",
        action="append",
        dest="countries",
        choices=list(calibration_trees.RELEASE_REPOS),
        help="Backfill only this country; repeat for several. Every country by default.",
    )
    backfill.add_argument(
        "--kind",
        choices=["release", "staging"],
        help="Backfill only release or only staging builds. Both by default.",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "List what would be built, downloading no diagnostics, building "
            "nothing and taking no lock."
        ),
    )
    reconcile = commands.add_parser(
        RECONCILE_COMMAND,
        help="Build a country's calibration trees that its Hugging Face repos hold.",
    )
    reconcile.add_argument(
        "--country",
        required=True,
        choices=list(calibration_trees.RELEASE_REPOS),
        help="The country to reconcile.",
    )
    reconcile.add_argument(
        "--kind",
        choices=["release", "staging"],
        help="Reconcile only release or only staging builds. Every kind by default.",
    )
    reconcile.add_argument(
        "--request-id",
        metavar="TASK_ID",
        help="The queued request this run answers, whose row it completes.",
    )
    reconcile.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "List what would be built, downloading no diagnostics, building "
            "nothing and taking no lock."
        ),
    )
    return parser


def _reconcile_argument_error(options: argparse.Namespace) -> str | None:
    """Why the reconcile command's arguments can't run together, if they can't."""

    if options.kind == "staging" and options.country not in (
        calibration_trees.STAGING_REPOS
    ):
        return f"--kind staging: {options.country} has no staging repo."
    if options.request_id is None:
        return None
    if options.dry_run:
        return "--request-id can't be used with --dry-run."
    if options.kind is None:
        return "--request-id needs --kind."
    if not calibration_tree_builds.is_request_id(
        options.request_id, options.country, options.kind
    ):
        return (
            "--request-id must be a calibration-trees-<country>-<kind>-<window> "
            "task ID of the same country and kind."
        )
    return None


def _is_https_address(value: str) -> bool:
    try:
        address = urlsplit(value)
    except ValueError:
        return False
    return address.scheme == "https" and bool(address.hostname)


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
    unpublished_run_ids: list[str] = []
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
                if outcome.outcome in UNPUBLISHED_OUTCOMES:
                    unpublished_run_ids.append(outcome.run_id)
        finally:
            engine.dispose()
    # The summary says whether the run is complete; a task retry starts the
    # inventory again, so only a summary without incomplete builds says so.
    _print_json(
        {
            "summary": {
                "outcomes": dict(outcome_counts),
                "incomplete_run_ids": incomplete_run_ids,
                "unpublished_run_ids": unpublished_run_ids,
            }
        }
    )
    return 1 if incomplete_run_ids else 0


def _backfill_calibration_trees(database_url: str, options: argparse.Namespace) -> int:
    blob_token_setting = calibration_trees.BLOB_TOKEN_SETTING
    blob_token = os.environ.get(blob_token_setting, "").strip()
    if not blob_token:
        raise RuntimeError(
            f"{blob_token_setting} is required when {BACKFILL_REQUIREMENT}."
        )
    countries = list(options.countries or calibration_trees.RELEASE_REPOS)
    kinds = (
        [options.kind] if options.kind else list(calibration_tree_backfill.BUILD_KINDS)
    )
    outcomes = []
    with (
        HuggingFaceReader.from_environment() as reader,
        # Production's manifest is public: this client sends no credential.
        httpx.Client(
            timeout=calibration_tree_backfill.PRODUCTION_MANIFEST_TIMEOUT_SECONDS
        ) as production_client,
    ):
        engine = create_database_engine(database_url)
        try:
            commands = calibration_trees.tree_commands(
                blob_token=blob_token,
                manifest_lock=calibration_trees.postgres_manifest_lock(engine),
                size_limits=calibration_trees.size_limits_from(os.environ),
            )
            # Each outcome is printed as it is produced, so a run that stops
            # early still logs every entry it finished.
            for outcome in calibration_tree_backfill.backfill_calibration_trees(
                production_manifest_url=options.production_manifest_url,
                reader=reader,
                production_client=production_client,
                commands=commands,
                countries=countries,
                kinds=kinds,
                dry_run=options.dry_run,
            ):
                _print_json(outcome.to_json())
                outcomes.append(outcome)
        finally:
            engine.dispose()
    summary = calibration_tree_backfill.summarize(outcomes, countries)
    _print_json({"summary": summary})
    return 1 if summary["incomplete"] else 0


def _reconcile_kinds(options: argparse.Namespace) -> list[str]:
    if options.kind is not None:
        return [options.kind]
    return [
        kind
        for kind in calibration_tree_backfill.BUILD_KINDS
        if kind == "release" or options.country in calibration_trees.STAGING_REPOS
    ]


def _reconcile_calibration_trees(database_url: str, options: argparse.Namespace) -> int:
    reconciliation = calibration_tree_reconciliation
    blob_token_setting = calibration_trees.BLOB_TOKEN_SETTING
    blob_token = os.environ.get(blob_token_setting, "").strip()
    if not blob_token:
        raise RuntimeError(
            f"{blob_token_setting} is required when {RECONCILE_REQUIREMENT}."
        )
    kinds = _reconcile_kinds(options)
    request_id: str | None = options.request_id
    outcomes: list[
        calibration_tree_reconciliation.ReconcileOutcome
        | calibration_tree_reconciliation.LatestStep
    ] = []

    def summary_of_outcomes() -> dict[str, Any]:
        return reconciliation.summarize(
            outcomes,
            country=options.country,
            kinds=kinds,
            request_id=request_id,
            dry_run=options.dry_run,
        )

    with HuggingFaceReader.from_environment() as reader:
        engine = create_database_engine(database_url)
        try:
            request_rows = calibration_tree_builds.PostgresCalibrationTreeBuildRequests(
                engine
            )
            manifest_lock = calibration_trees.postgres_manifest_lock(engine)
            reconciled = reconciliation.reconcile_calibration_trees(
                country=options.country,
                kinds=kinds,
                reader=reader,
                commands=calibration_trees.tree_commands(
                    blob_token=blob_token,
                    manifest_lock=manifest_lock,
                    size_limits=calibration_trees.size_limits_from(os.environ),
                ),
                submissions=reconciliation.PostgresSubmissionsInProgress(engine),
                lock=reconciliation.postgres_reconciliation_lock(engine),
                # A release run's last step: the country's latest follows
                # releases.json, under the manifest lock it takes itself.
                follow_latest=functools.partial(
                    calibration_trees.follow_releases_record,
                    blob_token=blob_token,
                    manifest_lock=manifest_lock,
                    reader=reader,
                ),
                dry_run=options.dry_run,
            )
            if request_id is not None:
                request_rows.record_start(
                    request_id,
                    execution=os.environ.get("CLOUD_RUN_EXECUTION") or None,
                    at=datetime.now(UTC),
                )
            try:
                # Each outcome is printed as it is produced, so a run that
                # stops early still logs every build it finished.
                for outcome in reconciled:
                    _print_json(outcome.to_json())
                    outcomes.append(outcome)
            except Exception as error:
                # The request's row records how the attempt ended before the
                # error ends the execution.
                if request_id is not None:
                    request_rows.record_finish(
                        request_id,
                        at=datetime.now(UTC),
                        summary={
                            **summary_of_outcomes(),
                            "incomplete": True,
                            "stopped_by": type(error).__name__,
                        },
                    )
                raise
            summary = summary_of_outcomes()
            _print_json({"summary": summary})
            if request_id is not None:
                request_rows.record_finish(
                    request_id, at=datetime.now(UTC), summary=summary
                )
        finally:
            engine.dispose()
    return 1 if summary["incomplete"] else 0


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one operator command.

    Non-zero when a reprocessing was refused, when projecting past builds
    left any build incomplete, or when a backfill or a reconciliation of
    calibration trees is incomplete; 2 for an argument error.
    """

    parser = _argument_parser()
    options = parser.parse_args(arguments)
    if options.command == BACKFILL_COMMAND and not _is_https_address(
        options.production_manifest_url
    ):
        parser.error("--production-manifest-url must be an https:// address.")
    if options.command == RECONCILE_COMMAND:
        reconcile_error = _reconcile_argument_error(options)
        if reconcile_error is not None:
            parser.error(reconcile_error)
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
    if options.command == BACKFILL_COMMAND:
        return _backfill_calibration_trees(database_url, options)
    if options.command == RECONCILE_COMMAND:
        return _reconcile_calibration_trees(database_url, options)
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
