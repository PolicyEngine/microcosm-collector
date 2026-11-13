"""Reconcile a country's calibration trees with its Hugging Face repos.

``python -m telemetry_collector.operations reconcile-calibration-trees
--country <cc> [--kind release|staging] [--request-id <task ID>] [--dry-run]``
compares the country's builds on Hugging Face with the collector's tree
store and builds what the store lacks. A Cloud Task that
``POST /v1/calibration-tree-builds`` queues runs it as an execution of the
worker job (``telemetry_collector.calibration_tree_builds``); an operator can
run it the same way.

**Which builds.** Each repo is read at its head commit, with the listing rules
of the GitHub Actions publisher (``packages/calibration-tree``) ported to
Python:

- **Release:** every build the release repo's ``releases.json`` lists,
  withdrawn ones included (``enumerateRecordedBuildCandidates``), parsed by
  ``telemetry_collector.releases_record``. A build the store holds under its
  release ID is ``already_stored``, with no further read. Any other build's
  commit is the newest last commit among its folder's files
  (``HuggingFaceReader.last_commits``); the folder must hold
  ``calibration_diagnostics.json``, and its ``release_manifest.json``, read
  at that commit, must name the same release and build. Its time is the
  record's ``published_at``, as ``recordedPublicationTime`` writes it. A repo
  without ``releases.json`` is ``no_releases_record`` and builds nothing; its
  tags and pointer files are not read. A ``releases.json`` the rules refuse is
  ``releases_record_refused`` and builds nothing.
- **Staging:** the runs of the staging repo's ``runs/<id>/`` folders, as
  ``loadStagingRuns`` finds them. A run the store holds is ``already_stored``,
  and its files are not read. Of the others, a structured run's status is its
  run manifest's; a schema-1 run's is the one ``runs.json`` records, else its
  ``progress.json``'s; a folder without a run manifest counts as a schema-1
  run when ``runs.json`` is schema 1. ``runs.json`` is read whenever a run
  the store lacks has a schema-1 manifest or none; the publisher reads it
  when any listed run's manifest is schema 1, and stored runs' manifests
  are not read. A ``runs.json`` that isn't JSON, or isn't a JSON object, is
  no index, as the publisher takes it. One that can't be read makes each
  run the store lacks with a schema-1 manifest or none an ``error``, and
  the run incomplete; the structured runs are still built. Only a run
  whose status is ``passed``, ``published`` or ``completed`` is built. Its
  label is its candidate release, else its run ID, and its time its
  progress's ``updated_at``, else its run manifest's, as the publisher's
  ``publishStagingSource`` takes them. A final run with no
  calibration diagnostics to build from is ``ineligible``, as the
  publisher's ``reconcileStagingBuilds`` reports it: a schema-1 folder
  without ``calibration_diagnostics.json``, a structured run declaring
  neither diagnostics nor a staged dataset, a staged-dataset receipt of a
  skipped, failed or disabled staging, or a bundle recording no diagnostics
  (``SourceWithoutDiagnosticsError``). A run whose documents can't be read,
  or whose declared or recorded diagnostics are absent or differ, is
  ``unbuildable`` on its own, and the other runs continue.

A build whose folder (``releases/<release>/<build>`` or ``staged/<run>``) a
``queued`` or ``processing`` ``calibration-diagnostics`` submission holds is
``submission_in_progress``: the submission builds it.

**Building.** With the backfill's steps (``_release_source``,
``_staging_source`` and ``_build``): one build at a time, the current default
build first, a file over 256 MiB ``too_large``, and no build started after 40
minutes of the execution (``deferred``). Nothing this command publishes asks
to become its country's latest (``make_latest`` false, with no
``decide_latest``).

**The latest.** A release run ends with one more step
(``_Reconciliation.latest_step``): it calls ``follow_latest``, which the
operations command binds to ``calibration_trees.follow_releases_record``
for the country, so the country's latest follows the current build of
``releases.json`` at the release repo's head, whatever its age, and nothing
is built for it. The step runs after the release builds, so the current
default build this run built can become the latest, and inside the
country's and kind's lock, so an execution queued after a change of
``releases.json`` runs its step after any earlier execution's. It takes the
manifest lock itself, so it runs outside it; the reconciliation lock is
taken first and only by this command, so the two locks are always taken
in that order. Its document, ``{"latest": {...}}``, and the summary's
``latest`` say what moved or why the latest stayed, with
``FollowedLatest``'s fields. A ``ValueError`` (a country the collector
doesn't list) is final; any other failure leaves the run incomplete, so
Cloud Run tries again. A release repo without ``releases.json``, a dry run
and a lock wait that expires skip the step: the execution that held the
lock runs its own step when its release builds end.

**Locks.** Each kind is reconciled inside a Postgres session advisory lock of
its own country and kind, held on one connection: an execution waits for it
up to 15 minutes, so that one that started after an event lists Hugging Face
only after any earlier one has finished; past the wait the kind is
``lock_wait_expired`` and the run incomplete. The store's manifest is read
once for each kind, inside its lock, with the package's read-only manifest
read. A dry run takes no lock, downloads no diagnostics, builds nothing,
reports ``would_build`` and skips the latest step.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from telemetry_collector import calibration_tree_backfill as backfill
from telemetry_collector.calibration_tree_backfill import (
    BUILD_KINDS,
    BUILD_TIME_LIMIT_SECONDS,
    BuildKind,
    ProductionEntry,
    SourceUnbuildableError,
    SourceWithoutDiagnosticsError,
    _build,
    _Listings,
    _read_json,
    _release_source,
    _staging_source,
)
from telemetry_collector.calibration_trees import (
    RELEASE_REPOS,
    SAFE_IDENTIFIER,
    STAGING_REPOS,
    FollowedLatest,
    TreeCommands,
    TreeRefusedError,
    TreeUnbuildableError,
    read_store_manifest,
    shortened_message,
)
from telemetry_collector.database import ArtifactSubmission
from telemetry_collector.huggingface_read import (
    HuggingFaceReader,
    HuggingFaceReadError,
    is_missing_path,
)
from telemetry_collector.releases_record import (
    RELEASES_RECORD_PATH,
    RecordedBuild,
    ReleasesRecordRefusedError,
    parse_releases_record,
)

ReconcileOutcomeKind = Literal[
    "would_build",
    "built",
    "already_stored",
    "submission_in_progress",
    "ineligible",
    "unbuildable",
    "too_large",
    "refused",
    "error",
    "deferred",
    "no_releases_record",
    "releases_record_refused",
    "lock_wait_expired",
]

#: What the latest step did: ``follow_releases_record`` answered, it was not
#: called, or it raised.
LatestStepOutcome = Literal["followed", "skipped", "error"]
#: Why a release run did not call ``follow_releases_record``.
LatestStepSkipped = Literal["dry_run", "no_releases_record", "lock_wait_expired"]
#: ``calibration_trees.follow_releases_record`` for one country, with the
#: worker job's Blob token, the manifest lock and the Hugging Face reader.
FollowLatest = Callable[[str], FollowedLatest]

#: How long an execution waits for another one's lock on the same country
#: and kind, and how often it tries.
LOCK_WAIT_SECONDS: Final = 15 * 60
LOCK_POLL_SECONDS: Final = 5.0
#: The staging statuses the publisher builds (FINAL_STAGING_STATUSES).
FINAL_STAGING_STATUSES: Final = frozenset({"passed", "published", "completed"})
#: The submissions that still build their own tree.
IN_PROGRESS_STATUSES: Final = ("queued", "processing")
_RUN_FILE: Final = re.compile(r"runs/([A-Za-z0-9][A-Za-z0-9._-]*)/.+")
_RUN_MANIFEST: Final = re.compile(
    r"runs/([A-Za-z0-9][A-Za-z0-9._-]*)/run_manifest\.json"
)
_STRUCTURED_MANIFEST_SCHEMA: Final = "microcosm.staging.run-manifest"
_STRUCTURED_SCHEMA_VERSIONS: Final = (2, 3)
_RELEASE_DIAGNOSTICS: Final = "calibration_diagnostics.json"
_RELEASE_MANIFEST: Final = "release_manifest.json"


@dataclass(frozen=True)
class ReconcileOutcome:
    """What the command did with one build, or with a whole kind.

    ``source_id`` is the build's release ID (``<release>/<build>``) or
    staging run; an outcome for a whole kind has none.
    """

    country: str
    kind: BuildKind
    outcome: ReconcileOutcomeKind
    source_id: str | None = None
    reason: str | None = None
    hf_repo: str | None = None
    hf_commit: str | None = None
    created_at: str | None = None
    #: A staging build's label.
    label: str | None = None
    #: The release build is ``releases.json``'s current default build.
    current_default: bool = False
    #: The release build is withdrawn; ``None`` for a staging build.
    withdrawn: bool | None = None
    #: The build ID built, or already stored.
    build_artifact_id: str | None = None
    uploaded_parts: int | None = None
    kept_parts: int | None = None
    #: A later run should try it again.
    incomplete: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LatestStep:
    """What a release run's last step did to its country's latest tree.

    ``followed``: ``follow_releases_record`` answered, and ``followed`` says
    what moved or why the latest stayed. ``skipped``: it was not called, and
    ``skipped`` says why. ``error``: it raised; the run is incomplete unless
    calling again can't help.
    """

    country: str
    outcome: LatestStepOutcome
    followed: FollowedLatest | None = None
    skipped: LatestStepSkipped | None = None
    #: Why the step was skipped, or what it raised.
    reason: str | None = None
    #: A later run should try it again.
    incomplete: bool = False

    def fields(self) -> dict[str, Any]:
        """The step as the summary's ``latest`` reports it."""

        document: dict[str, Any] = {"country": self.country, "outcome": self.outcome}
        if self.followed is not None:
            document.update(self.followed.result())
        if self.skipped is not None:
            document["skipped"] = self.skipped
        if self.reason is not None:
            document["reason"] = self.reason
        document["incomplete"] = self.incomplete
        return document

    def to_json(self) -> dict[str, Any]:
        return {"latest": self.fields()}


class SubmissionsInProgress(Protocol):
    """Whether a submission still builds a folder's tree."""

    def in_progress(self, country: str, folder: str) -> bool:
        """A ``queued`` or ``processing`` ``calibration-diagnostics``
        submission holds ``folder`` of one of ``country``'s repos."""


class ReconciliationLockTimeoutError(RuntimeError):
    """Another execution held the country's and kind's lock past the wait."""


ReconciliationLock = Callable[[str, str], AbstractContextManager[None]]


def _string(value: object) -> str | None:
    """A non-blank string, as the staging readers' ``stringValue`` takes one."""

    return value if isinstance(value, str) and value.strip() else None


def _instant(text: str) -> datetime:
    """The instant ``text`` names; a time without an offset is taken as UTC,
    as Cloud Run's clock reads one."""

    instant = datetime.fromisoformat(text)
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)


def publication_time(text: str) -> str | None:
    """``text`` as ``new Date(Date.parse(text)).toISOString()`` writes it, or
    ``None`` when it names no time: the publisher's ``recordedPublicationTime``."""

    try:
        instant = _instant(text).astimezone(UTC)
    except ValueError:
        return None
    return (
        instant.strftime("%Y-%m-%dT%H:%M:%S.") + f"{instant.microsecond // 1000:03d}Z"
    )


def _stored_builds(
    manifest: Mapping[str, Any], country: str, kind: str
) -> dict[str, Mapping[str, Any]]:
    """The builds the store's manifest holds for ``country`` and ``kind``, by
    release ID or staging run."""

    countries = manifest.get("countries")
    stored = countries.get(country) if isinstance(countries, dict) else None
    builds = stored.get("builds") if isinstance(stored, dict) else None
    alias = "releaseId" if kind == "release" else "stagingRunId"
    return {
        build[alias]: build
        for build in builds or []
        if isinstance(build, dict)
        and build.get("kind") == kind
        and isinstance(build.get(alias), str)
    }


def _described(error: BaseException) -> str:
    return shortened_message(f"{type(error).__name__}: {error}")


@dataclass(frozen=True)
class _Planned:
    """A build to build, with what its outcome reports."""

    entry: ProductionEntry
    current_default: bool = False
    withdrawn: bool | None = None
    #: The newest first: a staging run's time, then its ID.
    order: tuple[float, str] = (0.0, "")


@dataclass
class _StagingRun:
    """What the listing rules read of one staging run."""

    run_id: str
    run_manifest: Mapping[str, Any] | None = None
    progress: Mapping[str, Any] | None = None
    progress_read: bool = False
    structured: bool = False


@dataclass
class _Reconciliation:
    country: str
    reader: HuggingFaceReader
    commands: TreeCommands
    submissions: SubmissionsInProgress
    follow_latest: FollowLatest
    dry_run: bool
    clock: Callable[[], float]
    started: float
    time_limit_seconds: float
    listings: _Listings = field(init=False)

    def __post_init__(self) -> None:
        self.listings = _Listings(self.reader)

    def outcome(
        self, kind: BuildKind, result: ReconcileOutcomeKind, **fields: Any
    ) -> ReconcileOutcome:
        return ReconcileOutcome(
            country=self.country, kind=kind, outcome=result, **fields
        )

    # One kind ----------------------------------------------------------------

    def reconcile_kind(self, kind: BuildKind) -> Iterator[ReconcileOutcome]:
        try:
            manifest = read_store_manifest(self.commands)
        except Exception as error:
            yield self.outcome(
                kind,
                "error",
                reason=f"The store's manifest could not be read: {_described(error)}",
                incomplete=True,
            )
            return
        stored = _stored_builds(manifest, self.country, kind)
        planned = (
            self.release_plan(stored)
            if kind == "release"
            else self.staging_plan(stored)
        )
        for item in planned:
            yield item if isinstance(item, ReconcileOutcome) else self.build(item)

    # Releases ----------------------------------------------------------------

    def release_plan(
        self, stored: Mapping[str, Mapping[str, Any]]
    ) -> list[ReconcileOutcome | _Planned]:
        """Each listed build's final outcome, or its plan, in the order they
        are built: the current default build first, then the record's order."""

        repo = RELEASE_REPOS[self.country]
        try:
            head = self.reader.head_commit(repo)
            content = self.reader.read_file(repo, RELEASES_RECORD_PATH, head)
        except HuggingFaceReadError as error:
            if is_missing_path(error):
                return [
                    self.outcome(
                        "release",
                        "no_releases_record",
                        hf_repo=repo,
                        reason=(
                            f"{repo} has no {RELEASES_RECORD_PATH}; its tags and "
                            "pointer files are not read."
                        ),
                    )
                ]
            return [
                self.outcome(
                    "release",
                    "error",
                    hf_repo=repo,
                    reason=_described(error),
                    incomplete=True,
                )
            ]
        try:
            record = parse_releases_record(content)
        except ReleasesRecordRefusedError as error:
            return [
                self.outcome(
                    "release",
                    "releases_record_refused",
                    hf_repo=repo,
                    hf_commit=head,
                    reason=shortened_message(str(error)),
                )
            ]
        # The current default build first, so that the time limit never
        # defers it behind the others.
        builds = sorted(record.builds, key=lambda build: not build.current_default)
        return [
            self.release_build(repo, head, build, stored.get(build.build_id))
            for build in builds
        ]

    def release_build(
        self,
        repo: str,
        head: str,
        build: RecordedBuild,
        stored: Mapping[str, Any] | None,
    ) -> ReconcileOutcome | _Planned:
        reported = {
            "source_id": build.build_id,
            "hf_repo": repo,
            "current_default": build.current_default,
            "withdrawn": build.withdrawn,
        }
        folder = f"releases/{build.build_id}"
        if stored is not None:
            return self.outcome(
                "release",
                "already_stored",
                **{**reported, "hf_repo": stored.get("hfRepo", repo)},
                hf_commit=stored.get("hfCommitSha"),
                created_at=stored.get("createdAt"),
                build_artifact_id=stored.get("buildArtifactId"),
            )
        try:
            if self.submissions.in_progress(self.country, folder):
                return self.outcome(
                    "release",
                    "submission_in_progress",
                    reason=f"A submission of {folder} is still being processed.",
                    **reported,
                )
            listed = self.listings.folder(repo, head, folder)
            if _RELEASE_DIAGNOSTICS not in listed:
                return self.outcome(
                    "release",
                    "ineligible",
                    reason=f"{folder}/ has no calibration diagnostics.",
                    **reported,
                )
            commit = self.folder_commit(repo, head, folder, listed.values())
            manifest_path = f"{folder}/{_RELEASE_MANIFEST}"
            try:
                read = _read_json(self.reader, repo, manifest_path, commit)
            except SourceUnbuildableError:
                read = None
            manifest = None if read is None else read[0]
            if (
                manifest is None
                or manifest.get("release") != build.release
                or manifest.get("build") != build.build
            ):
                return self.outcome(
                    "release",
                    "ineligible",
                    reason=(
                        f"{manifest_path} does not name release {build.release} and "
                        f"build {build.build}."
                    ),
                    **{**reported, "hf_commit": commit},
                )
        except Exception as error:
            return self.outcome(
                "release",
                "error",
                reason=_described(error),
                incomplete=True,
                **reported,
            )
        return _Planned(
            entry=ProductionEntry(
                country=self.country,
                kind="release",
                # No production build ID: the store gives the build its own.
                build_artifact_id="",
                source_id=build.build_id,
                label=build.build_id,
                release_directory=build.build_id,
                hf_repo=repo,
                hf_commit=commit,
                created_at=publication_time(build.published_at),
                make_latest=False,
            ),
            current_default=build.current_default,
            withdrawn=build.withdrawn,
        )

    def folder_commit(
        self, repo: str, head: str, folder: str, listed: Iterable[Any]
    ) -> str:
        """The newest last commit among the folder's files at ``head``: the
        commit that wrote the build folder, which is never changed after."""

        last_commits = self.reader.last_commits(repo, folder, head)
        newest: tuple[datetime, str] | None = None
        for dataset_file in listed:
            last_commit = last_commits.get(dataset_file.path)
            if last_commit is None:
                raise HuggingFaceReadError(
                    repo,
                    200,
                    f"Hugging Face listed no last commit for {dataset_file.path}.",
                )
            when = _instant(last_commit.date)
            # The first listed of the newest, as the publisher's stable sort
            # keeps it.
            if newest is None or when > newest[0]:
                newest = (when, last_commit.commit)
        assert newest is not None, "The folder lists its diagnostics."
        return newest[1]

    # Staging -----------------------------------------------------------------

    def staging_plan(
        self, stored: Mapping[str, Mapping[str, Any]]
    ) -> list[ReconcileOutcome | _Planned]:
        """Each run's final outcome, then the plans of the runs to build, the
        newest first, as the staging readers order runs."""

        repo = STAGING_REPOS[self.country]
        try:
            head = self.reader.head_commit(repo)
            paths = [listed.path for listed in self.reader.list_files(repo, head)]
        except HuggingFaceReadError as error:
            return [
                self.outcome(
                    "staging",
                    "error",
                    hf_repo=repo,
                    reason=_described(error),
                    incomplete=True,
                )
            ]
        run_ids = sorted(
            {match[1] for path in paths if (match := _RUN_FILE.fullmatch(path))}
        )
        with_manifest = {
            match[1] for path in paths if (match := _RUN_MANIFEST.fullmatch(path))
        }
        finished: list[ReconcileOutcome] = []
        runs: list[_StagingRun] = []
        for run_id in run_ids:
            if run_id in stored:
                build = stored[run_id]
                finished.append(
                    self.outcome(
                        "staging",
                        "already_stored",
                        source_id=run_id,
                        hf_repo=build.get("hfRepo", repo),
                        hf_commit=build.get("hfCommitSha"),
                        created_at=build.get("createdAt"),
                        label=build.get("label"),
                        build_artifact_id=build.get("buildArtifactId"),
                    )
                )
                continue
            if SAFE_IDENTIFIER.fullmatch(run_id) is None or run_id == "latest":
                finished.append(
                    self.outcome(
                        "staging",
                        "unbuildable",
                        source_id=run_id,
                        hf_repo=repo,
                        hf_commit=head,
                        reason=f"Run folder {run_id} is not a run ID the tree worker takes.",
                    )
                )
                continue
            run = _StagingRun(run_id)
            if run_id in with_manifest:
                try:
                    self.read_run_manifest(repo, head, run)
                except SourceUnbuildableError as error:
                    finished.append(
                        self.outcome(
                            "staging",
                            "unbuildable",
                            source_id=run_id,
                            hf_repo=repo,
                            hf_commit=head,
                            reason=shortened_message(str(error)),
                        )
                    )
                    continue
                except Exception as error:
                    finished.append(
                        self.outcome(
                            "staging",
                            "error",
                            source_id=run_id,
                            hf_repo=repo,
                            reason=_described(error),
                            incomplete=True,
                        )
                    )
                    continue
            runs.append(run)
        # A schema-1 runs.json records schema-1 runs' statuses, and makes
        # every run folder without a structured manifest a schema-1 run. The
        # staging readers read it when any listed run's manifest is schema 1.
        # Stored runs' manifests are never read (S7), so it is read whenever
        # a run the store lacks may be a schema-1 run: its manifest is schema
        # 1, or it has none, whether or not the other runs' manifests are.
        index: dict[str, str | None] | None = None
        index_failure: str | None = None
        if any(not run.structured for run in runs):
            try:
                index = self.run_index(repo, head)
            except Exception as error:
                index_failure = f"runs.json could not be read: {_described(error)}"
        planned: list[_Planned] = []
        for run in runs:
            if index_failure is not None and not run.structured:
                # A run with a schema-1 manifest or none may take its status
                # from runs.json, and a folder without a run manifest is a run
                # only beside a schema-1 runs.json. A structured run's status
                # is its manifest's, so structured runs are still planned.
                finished.append(
                    self.outcome(
                        "staging",
                        "error",
                        source_id=run.run_id,
                        hf_repo=repo,
                        reason=index_failure,
                        incomplete=True,
                    )
                )
                continue
            if run.run_manifest is None and index is None:
                # Not a run by the staging readers' rules.
                continue
            result = self.staging_run(repo, head, run, index)
            if isinstance(result, _Planned):
                planned.append(result)
            else:
                finished.append(result)
        planned.sort(key=lambda build: build.order, reverse=True)
        return [*finished, *planned]

    def read_run_manifest(self, repo: str, head: str, run: _StagingRun) -> None:
        """Read and classify a run's manifest; ``SourceUnbuildableError`` for one
        the staging readers refuse."""

        read = _read_json(
            self.reader, repo, f"runs/{run.run_id}/run_manifest.json", head
        )
        if read is None:
            return
        manifest = read[0]
        version = manifest.get("schema_version")
        if manifest.get("run_id") != run.run_id:
            raise SourceUnbuildableError(
                f"Run {run.run_id}'s manifest names another run."
            )
        if isinstance(version, bool):
            raise SourceUnbuildableError(
                f"Run {run.run_id}'s manifest has an unsupported schema version."
            )
        if version == 1 and manifest.get("schema_name") is None:
            run.structured = False
        elif (
            version in _STRUCTURED_SCHEMA_VERSIONS
            and manifest.get("schema_name") == _STRUCTURED_MANIFEST_SCHEMA
        ):
            run.structured = True
        else:
            raise SourceUnbuildableError(
                f"Run {run.run_id}'s manifest has an unsupported schema version."
            )
        run.run_manifest = manifest

    def run_index(self, repo: str, head: str) -> dict[str, str | None] | None:
        """Each run's status in a schema-1 ``runs.json``; ``None`` without one.

        A ``runs.json`` that isn't JSON, or isn't a JSON object, is none, as
        the staging readers' ``stagingJsonOrNull`` and ``asObject`` take it.
        A failed read raises ``HuggingFaceReadError``.
        """

        try:
            read = _read_json(self.reader, repo, "runs.json", head)
        except SourceUnbuildableError:
            return None
        if read is None:
            return None
        index = read[0]
        rows = index.get("runs")
        if (
            index.get("schema_version") != 1
            or isinstance(index.get("schema_version"), bool)
            or index.get("schema_name") is not None
            or not isinstance(rows, list)
        ):
            return None
        return {
            row["run_id"]: _string(row.get("status"))
            for row in rows
            if isinstance(row, dict) and _string(row.get("run_id"))
        }

    def progress(
        self, repo: str, head: str, run: _StagingRun
    ) -> Mapping[str, Any] | None:
        if not run.progress_read:
            read = _read_json(
                self.reader, repo, f"runs/{run.run_id}/progress.json", head
            )
            run.progress = None if read is None else read[0]
            run.progress_read = True
        return run.progress

    def staging_run(
        self,
        repo: str,
        head: str,
        run: _StagingRun,
        index: Mapping[str, str | None] | None,
    ) -> ReconcileOutcome | _Planned:
        run_id = run.run_id
        reported = {"source_id": run_id, "hf_repo": repo, "hf_commit": head}
        try:
            if run.structured:
                assert run.run_manifest is not None
                status = _string(run.run_manifest.get("status"))
            elif index is not None and run_id in index:
                status = index[run_id]
            else:
                progress = self.progress(repo, head, run)
                if progress is not None and progress.get("run_id") != run_id:
                    raise SourceUnbuildableError(
                        f"Run {run_id}'s progress names another run."
                    )
                status = None if progress is None else _string(progress.get("status"))
            if status not in FINAL_STAGING_STATUSES:
                return self.outcome(
                    "staging",
                    "ineligible",
                    reason=f"Run {run_id} is not final (status {status}).",
                    **reported,
                )
            folder = f"staged/{run_id}"
            if self.submissions.in_progress(self.country, folder):
                return self.outcome(
                    "staging",
                    "submission_in_progress",
                    reason=f"A submission of {folder} is still being processed.",
                    **reported,
                )
            progress = self.progress(repo, head, run) or {}
            manifest = run.run_manifest or {}
            candidate_key = "candidate_id" if run.structured else "candidate_release_id"
            label = (
                _string(progress.get(candidate_key))
                or _string(manifest.get(candidate_key))
                or run_id
            )
            updated_at = _string(progress.get("updated_at")) or _string(
                manifest.get("updated_at")
            )
        except SourceUnbuildableError as error:
            return self.outcome(
                "staging",
                "unbuildable",
                reason=shortened_message(str(error)),
                **reported,
            )
        except Exception as error:
            return self.outcome(
                "staging",
                "error",
                reason=_described(error),
                incomplete=True,
                **reported,
            )
        created_at = None if updated_at is None else publication_time(updated_at)
        order_time = (
            float("-inf") if created_at is None else _instant(created_at).timestamp()
        )
        return _Planned(
            entry=ProductionEntry(
                country=self.country,
                kind="staging",
                # No production build ID: the store gives the build its own.
                build_artifact_id="",
                source_id=run_id,
                label=label,
                release_directory=None,
                hf_repo=repo,
                hf_commit=head,
                created_at=created_at,
                make_latest=False,
            ),
            order=(order_time, run_id),
        )

    # The latest --------------------------------------------------------------

    def latest_skipped(self, skipped: LatestStepSkipped, reason: str) -> LatestStep:
        return LatestStep(
            country=self.country, outcome="skipped", skipped=skipped, reason=reason
        )

    def latest_step(self, release_outcomes: Sequence[ReconcileOutcome]) -> LatestStep:
        """Make the country's latest follow releases.json, after the release
        builds and outside the manifest lock, which ``follow_latest`` takes."""

        if any(outcome.outcome == "no_releases_record" for outcome in release_outcomes):
            return self.latest_skipped(
                "no_releases_record",
                f"The release repo has no {RELEASES_RECORD_PATH}, so it names no "
                "current build to follow.",
            )
        try:
            followed = self.follow_latest(self.country)
        except ValueError as error:
            # A country the collector doesn't list: calling again can't help.
            return LatestStep(
                country=self.country, outcome="error", reason=_described(error)
            )
        except Exception as error:
            return LatestStep(
                country=self.country,
                outcome="error",
                reason=_described(error),
                incomplete=True,
            )
        return LatestStep(country=self.country, outcome="followed", followed=followed)

    # Building ----------------------------------------------------------------

    def build(self, planned: _Planned) -> ReconcileOutcome:
        entry = planned.entry
        kind: BuildKind = entry.kind

        def outcome(result: ReconcileOutcomeKind, **fields: Any) -> ReconcileOutcome:
            return self.outcome(
                kind,
                result,
                source_id=entry.source_id,
                hf_repo=entry.hf_repo,
                hf_commit=entry.hf_commit,
                created_at=entry.created_at,
                label=entry.label if kind == "staging" else None,
                current_default=planned.current_default,
                withdrawn=planned.withdrawn,
                **fields,
            )

        if not self.dry_run and self.clock() - self.started >= self.time_limit_seconds:
            return outcome(
                "deferred",
                reason="No build starts after the execution's time limit; run again.",
                incomplete=True,
            )
        try:
            source = (
                _release_source(entry, self.listings)
                if kind == "release"
                else _staging_source(entry, self.reader, self.listings)
            )
        except SourceWithoutDiagnosticsError as error:
            # As the publisher's reconcileStagingBuilds reports such a run.
            return outcome(
                "ineligible",
                reason=shortened_message(
                    f"Finalized staging run has no calibration diagnostics: {error}"
                ),
            )
        except SourceUnbuildableError as error:
            return outcome("unbuildable", reason=shortened_message(str(error)))
        except Exception as error:
            return outcome("error", reason=_described(error), incomplete=True)
        limit = backfill.MAX_SOURCE_FILE_BYTES
        oversized = [
            source_file.listed
            for source_file in source.files.values()
            if source_file.listed.size > limit
        ]
        if oversized:
            return outcome(
                "too_large",
                reason=(
                    f"{oversized[0].path} is {oversized[0].size} bytes, over the "
                    f"{limit}-byte limit."
                ),
            )
        if self.dry_run:
            return outcome("would_build")
        try:
            published = _build(source, self.reader, self.commands)
        except (SourceUnbuildableError, TreeUnbuildableError) as error:
            return outcome("unbuildable", reason=shortened_message(str(error)))
        except TreeRefusedError as error:
            return outcome("refused", reason=shortened_message(str(error)))
        except Exception as error:
            return outcome("error", reason=_described(error), incomplete=True)
        return outcome(
            "built",
            build_artifact_id=published.build_artifact_id,
            uploaded_parts=published.uploaded_parts,
            kept_parts=published.kept_parts,
        )


def reconcile_calibration_trees(
    *,
    country: str,
    kinds: Sequence[str],
    reader: HuggingFaceReader,
    commands: TreeCommands,
    submissions: SubmissionsInProgress,
    lock: ReconciliationLock,
    follow_latest: FollowLatest,
    dry_run: bool = False,
    clock: Callable[[], float] = time.monotonic,
    time_limit_seconds: float = BUILD_TIME_LIMIT_SECONDS,
) -> Iterator[ReconcileOutcome | LatestStep]:
    """Reconcile ``country``'s ``kinds``, in that order, yielding each outcome.

    The release kind ends with its ``LatestStep``: ``follow_latest`` for the
    country, called after the release builds and inside the release lock.

    Raises ``ValueError`` for a country the collector doesn't build, a kind
    that is neither release nor staging, or staging for a country without a
    staging repo.
    """

    if country not in RELEASE_REPOS:
        raise ValueError(f"{country} is not a country the collector builds trees for.")
    for kind in kinds:
        if kind not in BUILD_KINDS:
            raise ValueError(f"{kind} is neither release nor staging.")
        if kind == "staging" and country not in STAGING_REPOS:
            raise ValueError(f"{country} has no staging repo.")
    reconciliation = _Reconciliation(
        country=country,
        reader=reader,
        commands=commands,
        submissions=submissions,
        follow_latest=follow_latest,
        dry_run=dry_run,
        clock=clock,
        started=clock(),
        time_limit_seconds=time_limit_seconds,
    )
    for kind in kinds:
        if dry_run:
            yield from reconciliation.reconcile_kind(kind)
            if kind == "release":
                yield reconciliation.latest_skipped(
                    "dry_run", "A dry run does not move the latest."
                )
            continue
        try:
            with lock(country, kind):
                kind_outcomes: list[ReconcileOutcome] = []
                for outcome in reconciliation.reconcile_kind(kind):
                    kind_outcomes.append(outcome)
                    yield outcome
                if kind == "release":
                    yield reconciliation.latest_step(kind_outcomes)
        except ReconciliationLockTimeoutError as error:
            yield reconciliation.outcome(
                kind,
                "lock_wait_expired",
                reason=shortened_message(str(error)),
                incomplete=True,
            )
            if kind == "release":
                yield reconciliation.latest_skipped(
                    "lock_wait_expired",
                    "Another execution held the lock; it follows releases.json "
                    "when its release builds end.",
                )


def summarize(
    outcomes: Iterable[ReconcileOutcome | LatestStep],
    *,
    country: str,
    kinds: Sequence[str],
    request_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """The run's summary: its counts, and whether it is incomplete.

    ``outcomes`` counts the builds' and kinds' outcomes, and
    ``incomplete_sources`` lists those that are incomplete. ``latest`` is
    the release run's latest step (``LatestStep.fields``), or ``None`` for a
    run without releases or one that stopped before the step; the run is
    also incomplete when that step is.
    """

    listed = list(outcomes)
    builds = [outcome for outcome in listed if isinstance(outcome, ReconcileOutcome)]
    latest_steps = [outcome for outcome in listed if isinstance(outcome, LatestStep)]
    return {
        "country": country,
        "kinds": list(kinds),
        "request_id": request_id,
        "dry_run": dry_run,
        "outcomes": dict(Counter(outcome.outcome for outcome in builds)),
        "incomplete": any(outcome.incomplete for outcome in listed),
        "incomplete_sources": [
            {"kind": outcome.kind, "source_id": outcome.source_id}
            for outcome in builds
            if outcome.incomplete
        ],
        "latest": latest_steps[-1].fields() if latest_steps else None,
    }


# Postgres -------------------------------------------------------------------------


def reconciliation_lock_key(country: str, kind: str) -> int:
    """The advisory lock key of one country's and kind's reconciliation, apart
    from the manifest lock's (``MANIFEST_LOCK_KEY``)."""

    digest = hashlib.sha256(
        f"calibration-tree-reconciliation/{country}/{kind}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def postgres_reconciliation_lock(
    engine: Engine,
    *,
    wait_seconds: float = LOCK_WAIT_SECONDS,
    poll_seconds: float = LOCK_POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ReconciliationLock:
    """A session advisory lock per country and kind, held on one connection
    in autocommit mode, so that no transaction stays open while it is held.

    Tried every ``poll_seconds``; ``ReconciliationLockTimeoutError`` once
    ``wait_seconds`` have passed without it.
    """

    @contextmanager
    def lock(country: str, kind: str) -> Iterator[None]:
        key = reconciliation_lock_key(country, kind)
        with engine.connect() as connection:
            connection.execution_options(isolation_level="AUTOCOMMIT")
            deadline = clock() + wait_seconds
            while not connection.scalar(
                sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            ):
                if clock() >= deadline:
                    raise ReconciliationLockTimeoutError(
                        f"Another execution held the {country} {kind} lock for "
                        f"{wait_seconds:g} seconds."
                    )
                sleep(poll_seconds)
            try:
                yield
            finally:
                connection.execute(
                    sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )

    return lock


class PostgresSubmissionsInProgress:
    """The submissions table's ``queued`` and ``processing`` submissions."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def in_progress(self, country: str, folder: str) -> bool:
        repos = [RELEASE_REPOS[country]]
        if country in STAGING_REPOS:
            repos.append(STAGING_REPOS[country])
        provenance = ArtifactSubmission.provenance
        statement = (
            sa.select(ArtifactSubmission.submission_id)
            .where(
                ArtifactSubmission.kind == "calibration-diagnostics",
                ArtifactSubmission.status.in_(IN_PROGRESS_STATUSES),
                provenance[("huggingface", "folder")].astext == folder,
                provenance[("huggingface", "repo")].astext.in_(repos),
            )
            .limit(1)
        )
        with self._engine.connect() as connection:
            return connection.execute(statement).first() is not None
