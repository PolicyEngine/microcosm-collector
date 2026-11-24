"""The ``backfill-calibration-trees`` command: fill the calibration tree store.

The store must hold, before the dashboard reads everything from it, what the
collector's jobs do not reach on their own: each country's ``releases.json``,
every staging run older than the webhook-queued job covers
(``calibration_tree_reconciliation.JOB_COVERS_RUNS_FROM``), and the original
files of builds stored before the collector kept them. One execution runs
these steps, in this order, for the selected countries and kinds:

1. **releases.json** (release kind): each country's, at its release repo's head,
   through ``calibration_trees.copy_releases_record``. A dry run reads the
   stored copy's version, the head and its record, writes nothing, and
   reports ``would_store`` or ``unchanged``.
2. **Production's manifest**, only with its address: what it lists and the
   store lacks is built (``calibration_tree_backfill``). Since the dashboard
   reads the collector's production store, production's manifest lists that
   store itself, so the staging and production runs leave it out.
3. **Older staging runs** (staging kind): each country with a staging repo, by
   the reconcile command's code with ``include_older_runs``, so every run on
   Hugging Face, whatever its status, gets its files copied and, with
   diagnostics, a tree. A refusal the reconcile code recorded for an unchanged
   folder is repeated, not built again (``retry_refusals`` false), so a retry
   spends its time on what is left.
4. **Original files**: the copies missing for every build in the store's own
   manifest, read after the steps above
   (``calibration_tree_source_copies``).

Steps 2 to 4 share the execution's time limit: each is given what is left of
it, and works only while some is left, so a Cloud Run retry carries on past
what is stored. A staging country is ``deferred``, without waiting for its
lock or reading Hugging Face, unless at least the reconcile lock's 15-minute
wait and 5 more minutes are left (``STAGING_COUNTRY_MINIMUM_SECONDS``), and the
copy step defers each build before looking for its files once none is left.
A build or download already started runs to its end. A dry run has no time
limit, as each step's dry run has none. A step that fails for one country is reported
and the rest go on. Nothing here asks to move a country's latest, except the
builds of step 2, as that step always has.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any, Final, Protocol

import httpx

from telemetry_collector import (
    calibration_tree_backfill,
    calibration_tree_reconciliation,
    calibration_tree_source_copies,
)
from telemetry_collector.calibration_tree_backfill import (
    BUILD_KINDS,
    BUILD_TIME_LIMIT_SECONDS,
    BackfillOutcome,
)
from telemetry_collector.calibration_tree_reconciliation import (
    FollowLatest,
    ReconcileOutcome,
    ReconciliationLock,
    ReleasesRecordStep,
    SubmissionsInProgress,
)
from telemetry_collector.calibration_tree_source_copies import SourceFilesOutcome
from telemetry_collector.calibration_trees import (
    RELEASE_REPOS,
    STAGING_REPOS,
    CopiedReleasesRecord,
    TreeCommands,
    shortened_message,
)
from telemetry_collector.huggingface_read import HuggingFaceReader

#: A staging country starts only with this much of the execution's time left:
#: the reconcile code may wait this long for its lock before it looks at the
#: time, and the rest is for its work, so the execution ends within the worker
#: job's one-hour task.
STAGING_COUNTRY_MINIMUM_SECONDS: Final = (
    calibration_tree_reconciliation.LOCK_WAIT_SECONDS + 5 * 60
)
#: An outcome of any step.
StepOutcome = (
    ReleasesRecordStep | BackfillOutcome | ReconcileOutcome | SourceFilesOutcome
)


class CopyReleasesRecord(Protocol):
    """``calibration_trees.copy_releases_record`` for one country, with the
    worker job's Blob token and the Hugging Face reader."""

    def __call__(self, country: str, *, dry_run: bool = False) -> CopiedReleasesRecord:
        """Store, or in a dry run only check, ``country``'s copy."""


def _described(error: BaseException) -> str:
    return shortened_message(f"{type(error).__name__}: {error}")


def _releases_record(
    country: str, copy_record: CopyReleasesRecord, dry_run: bool
) -> ReleasesRecordStep:
    try:
        copied = copy_record(country, dry_run=dry_run)
    except Exception as error:
        return ReleasesRecordStep(
            country=country, outcome="error", reason=_described(error), incomplete=True
        )
    return ReleasesRecordStep(country=country, outcome=copied.outcome, copied=copied)


def backfill_calibration_tree_store(
    *,
    reader: HuggingFaceReader,
    commands: TreeCommands,
    copy_record: CopyReleasesRecord,
    submissions: SubmissionsInProgress,
    lock: ReconciliationLock,
    follow_latest: FollowLatest,
    countries: Sequence[str] = tuple(RELEASE_REPOS),
    kinds: Sequence[str] = BUILD_KINDS,
    dry_run: bool = False,
    production_manifest_url: str | None = None,
    production_client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    time_limit_seconds: float = BUILD_TIME_LIMIT_SECONDS,
) -> Iterator[StepOutcome]:
    """Run the backfill's steps, yielding each outcome as it is produced.

    ``submissions``, ``lock`` and ``follow_latest`` are the reconcile
    command's, for its staging runs; ``production_client`` is needed only with
    ``production_manifest_url``.
    """

    if production_manifest_url is not None and production_client is None:
        raise ValueError("Production's manifest needs a client to be read with.")
    started = clock()

    def time_left() -> float:
        return max(0.0, time_limit_seconds - (clock() - started))

    if "release" in kinds:
        for country in countries:
            yield _releases_record(country, copy_record, dry_run)
    if production_manifest_url is not None:
        assert production_client is not None
        yield from calibration_tree_backfill.backfill_calibration_trees(
            production_manifest_url=production_manifest_url,
            reader=reader,
            production_client=production_client,
            commands=commands,
            countries=countries,
            kinds=kinds,
            dry_run=dry_run,
            clock=clock,
            time_limit_seconds=time_left(),
        )
    if "staging" in kinds:
        for country in countries:
            if country not in STAGING_REPOS:
                continue
            if not dry_run and time_left() < STAGING_COUNTRY_MINIMUM_SECONDS:
                yield ReconcileOutcome(
                    country=country,
                    kind="staging",
                    outcome="deferred",
                    reason=(
                        "A staging country starts only with time left for the "
                        "reconcile lock's wait and some work; run again."
                    ),
                    incomplete=True,
                )
                continue
            yield from _older_staging_runs(
                country,
                reader=reader,
                commands=commands,
                submissions=submissions,
                lock=lock,
                follow_latest=follow_latest,
                copy_record=copy_record,
                dry_run=dry_run,
                clock=clock,
                time_limit_seconds=time_left(),
            )
    yield from calibration_tree_source_copies.store_missing_source_files(
        reader=reader,
        commands=commands,
        countries=countries,
        kinds=kinds,
        dry_run=dry_run,
        clock=clock,
        time_limit_seconds=time_left(),
    )


def _older_staging_runs(
    country: str,
    *,
    reader: HuggingFaceReader,
    commands: TreeCommands,
    submissions: SubmissionsInProgress,
    lock: ReconciliationLock,
    follow_latest: FollowLatest,
    copy_record: CopyReleasesRecord,
    dry_run: bool,
    clock: Callable[[], float],
    time_limit_seconds: float,
) -> Iterator[ReconcileOutcome]:
    """Every staging run of ``country``, by the reconcile command's code."""

    try:
        for outcome in calibration_tree_reconciliation.reconcile_calibration_trees(
            country=country,
            kinds=["staging"],
            reader=reader,
            commands=commands,
            submissions=submissions,
            lock=lock,
            follow_latest=follow_latest,
            copy_record=copy_record,
            dry_run=dry_run,
            clock=clock,
            time_limit_seconds=time_limit_seconds,
            include_older_runs=True,
            retry_refusals=False,
        ):
            # A staging run yields only its runs' outcomes.
            if isinstance(outcome, ReconcileOutcome):
                yield outcome
    except Exception as error:
        yield ReconcileOutcome(
            country=country,
            kind="staging",
            outcome="error",
            reason=f"The staging runs stopped: {_described(error)}",
            incomplete=True,
        )


def document(outcome: StepOutcome) -> dict[str, Any]:
    """One outcome as the command prints it, under its step's name."""

    if isinstance(outcome, ReleasesRecordStep):
        return outcome.to_json()
    if isinstance(outcome, BackfillOutcome):
        return {"production_build": outcome.to_json()}
    if isinstance(outcome, ReconcileOutcome):
        return {"staging_run": outcome.to_json()}
    return {"source_files": outcome.to_json()}


def summarize(
    outcomes: Iterable[StepOutcome],
    *,
    countries: Sequence[str],
    kinds: Sequence[str],
    dry_run: bool,
    production_builds: bool,
) -> dict[str, Any]:
    """The run's summary: one part per step, and whether anything is incomplete.

    ``releases_records`` is each country's ``releases.json`` step;
    ``production_builds`` the summary of production's manifest, or ``None``
    when its address was not given; ``staging_runs`` the reconcile summary of
    each country with a staging repo; ``source_files`` the copy step's.
    """

    listed = list(outcomes)
    records = [outcome for outcome in listed if isinstance(outcome, ReleasesRecordStep)]
    production = [outcome for outcome in listed if isinstance(outcome, BackfillOutcome)]
    staging = [outcome for outcome in listed if isinstance(outcome, ReconcileOutcome)]
    copies = [outcome for outcome in listed if isinstance(outcome, SourceFilesOutcome)]
    production_summary = (
        calibration_tree_backfill.summarize(production, countries)
        if production_builds
        else None
    )
    staging_summaries = {
        country: calibration_tree_reconciliation.summarize(
            [outcome for outcome in staging if outcome.country == country],
            country=country,
            kinds=["staging"],
            request_id=None,
            dry_run=dry_run,
        )
        for country in countries
        if "staging" in kinds and country in STAGING_REPOS
    }
    copies_summary = calibration_tree_source_copies.summarize(copies)
    return {
        "dry_run": dry_run,
        "incomplete": any(record.incomplete for record in records)
        or bool(production_summary and production_summary["incomplete"])
        or any(summary["incomplete"] for summary in staging_summaries.values())
        or copies_summary["incomplete"],
        "releases_records": {record.country: record.fields() for record in records},
        "production_builds": production_summary,
        "staging_runs": staging_summaries,
        "source_files": copies_summary,
    }
