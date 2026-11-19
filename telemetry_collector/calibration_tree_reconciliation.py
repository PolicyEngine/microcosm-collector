"""Reconcile a country's calibration trees with its Hugging Face repos.

``python -m telemetry_collector.operations reconcile-calibration-trees
--country <cc> [--kind release|staging] [--request-id <task ID>] [--dry-run]
[--include-older-runs]`` compares the country's builds on Hugging Face with the collector's tree
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
- **Staging:** the runs of the staging repo's ``runs/<id>/`` folders that this
  command covers, whatever their status. It covers a run that has a copy
  already, or whose folder last changed at or after ``JOB_COVERS_RUNS_FROM``,
  or a folder Hugging Face gives no last commit for; any other run is
  ``older_run``, left to the backfill, whose staging step runs this code with
  ``include_older_runs`` (``telemetry_collector.calibration_tree_store_backfill``).
  Together they leave no run out. A folder is a run
  when it holds ``run_manifest.json``, or, beside
  a schema-1 ``runs.json``, when it holds ``progress.json`` or the index lists
  it; any other folder is ``not_a_run``. ``runs.json`` is read only when a
  folder has no run manifest. One that isn't JSON, or isn't a JSON object, is no
  index, as the staging readers take it; one that can't be read makes each
  folder without a run manifest an incomplete ``error``.

  The files the dashboard reads are copied first
  (``telemetry_collector.staging_run_files``), in batches, at the head commit.
  A run whose folder has the fingerprint its record holds is not read again:
  ``run_files`` is ``unchanged``. Each run's outcome says what became of its
  files in ``run_files``: ``copied``, ``unchanged``, ``would_copy``,
  ``deferred`` or ``error``, the last two leaving the run incomplete. The job
  stores what the files say and works out no status of its own: an unfinished
  run's status comes from telemetry when a page is requested.

  A run with calibration diagnostics then has a tree, whatever its status. Its
  label is its candidate release, else its run ID, and its time its progress's
  ``updated_at``, else its run manifest's. A stored tree stays
  (``already_stored``) while the diagnostics and manifests the run has now match
  the digests its index records; a file only the tree has does not count. When
  one differs or is new, the run's tree is built again and replaces the stored
  one in the manifest, and ``replaced_build_artifact_id`` names the old build,
  whose parts stay stored. Commits are not compared: a run's folder usually gets
  further commits after its tree's. A run with no diagnostics to build from is
  ``ineligible``: a schema-1 folder without ``calibration_diagnostics.json``, a
  structured run declaring neither diagnostics nor a staged dataset, a
  staged-dataset receipt of a skipped, failed or disabled staging, or a bundle
  recording no diagnostics (``SourceWithoutDiagnosticsError``). A run whose
  documents can't be read, or whose declared or recorded diagnostics are absent
  or differ, is ``unbuildable`` on its own, and the other runs continue. A
  settled result, final refusals included, is kept in the run's record after
  each build, so the tree is looked at again only when the folder or the stored
  build changes, or for a refusal when ``retry_refusals`` asks (by default with
  ``include_older_runs``). A tree an
  earlier attempt stored under the same build ID is used as it is. A run whose
  folder is gone
  from the listing is marked removed, and readers leave it out until the folder
  returns.

A build whose folder (``releases/<release>/<build>`` or ``staged/<run>``) a
``queued`` or ``processing`` ``calibration-diagnostics`` submission holds is
``submission_in_progress``: the submission builds it. A submission that
arrives later finds the tree this command stored and succeeds as
``already_stored`` (``calibration_trees.TreeAlreadyStoredError``), and a
build whose submission stores its tree while this command builds it is
``already_stored`` here too: a release build's tree with the same build ID,
or any tree of the same staged run, which this command may build from other
files than its submission's: a staged bundle from its diagnostics alone, and
any other run from its ``runs/<id>/`` folder.

**Building.** Run files are copied before any tree is built, and no batch of
them starts after 40 minutes of the execution. With the backfill's steps
(``release_source``,
``staging_source`` and ``build_and_publish_source``): one build at a time,
the current default build first, a file over 256 MiB ``too_large``, and no
build started after 40 minutes of the execution (``deferred``). Nothing this
command publishes asks to become its country's latest (``make_latest``
false, with no ``decide_latest``).

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

**The stored releases.json.** A release run starts with one more step
(``_Reconciliation.releases_record_step``): it calls ``copy_record``, which
the operations command binds to ``calibration_trees.copy_releases_record``, so
the tree store holds the country's ``releases.json`` at the release repo's
head. The step comes before the run waits for its lock, so every release run
passes it, whatever its builds do and however long another execution holds
the lock, and a change of ``releases.json`` is not held back by a run that is
building. It needs no lock: its write succeeds only if the stored object is
the version read before the head was read. Its document,
``{"releases_record": {...}}``, and the summary's ``releases_record`` say what
it did, with ``CopiedReleasesRecord``'s fields. A failure leaves the run
incomplete, so Cloud Run tries again, and the builds go on. A dry run skips
the step and neither reads nor writes the object.

**Locks.** Each kind is reconciled inside a Postgres session advisory lock of
its own country and kind, held on one connection: an execution waits for it
up to 15 minutes, so that one that started after an event lists Hugging Face
only after any earlier one has finished; past the wait the kind is
``lock_wait_expired`` and the run incomplete. The store's manifest is read
once for each kind, inside its lock, with the package's read-only manifest
read. A dry run takes no lock, copies no run files, builds and stores nothing,
reports ``would_build`` and ``would_copy`` and skips the latest step. It reads
the documents it needs to plan, and no diagnostics stored with LFS or declared
with a digest.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Final, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from telemetry_collector import calibration_tree_backfill as backfill
from telemetry_collector import staging_run_files
from telemetry_collector.calibration_tree_backfill import (
    BUILD_KINDS,
    BUILD_TIME_LIMIT_SECONDS,
    MAX_COPIED_FILE_BYTES,
    BuildKind,
    BuildSource,
    Listings,
    ProductionEntry,
    SourceUnbuildableError,
    SourceWithoutDiagnosticsError,
    build_and_publish_source,
    read_json,
    release_source,
    staging_source,
)
from telemetry_collector.calibration_trees import (
    RELEASE_REPOS,
    SAFE_IDENTIFIER,
    STAGING_REPOS,
    CopiedReleasesRecord,
    FollowedLatest,
    TreeAlreadyStoredError,
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
    "not_a_run",
    "older_run",
]
#: What became of a staging run's copied files.
RunFilesOutcome = Literal["copied", "unchanged", "would_copy", "deferred", "error"]

#: What the latest step did: ``follow_releases_record`` answered, it was not
#: called, or it raised.
LatestStepOutcome = Literal["followed", "skipped", "error"]
#: Why a release run did not call ``follow_releases_record``.
LatestStepSkipped = Literal["dry_run", "no_releases_record", "lock_wait_expired"]
#: ``calibration_trees.follow_releases_record`` for one country, with the
#: worker job's Blob token, the manifest lock and the Hugging Face reader.
FollowLatest = Callable[[str], FollowedLatest]
#: What the step that stores releases.json did: what
#: ``copy_releases_record`` answered, that it was not called, or that it raised.
#: ``would_store`` comes only from the backfill's dry run, which calls
#: ``copy_releases_record`` with ``dry_run``.
ReleasesRecordStepOutcome = Literal[
    "stored", "unchanged", "refused", "would_store", "skipped", "error"
]
#: ``calibration_trees.copy_releases_record`` for one country, with the worker
#: job's Blob token and the Hugging Face reader.
CopyReleasesRecord = Callable[[str], CopiedReleasesRecord]

#: How long an execution waits for another one's lock on the same country
#: and kind, and how often it tries.
LOCK_WAIT_SECONDS: Final = 15 * 60
LOCK_POLL_SECONDS: Final = 5.0
#: The webhook-queued job covers staging runs changed from this time on. Older
#: runs reach the store through the backfill's staging step.
JOB_COVERS_RUNS_FROM: Final = datetime(2026, 11, 19, tzinfo=UTC)
#: The submissions that still build their own tree.
IN_PROGRESS_STATUSES: Final = ("queued", "processing")
#: Outcomes of a staging tree that took a build, after which results are saved.
_BUILD_ATTEMPTED: Final = frozenset({"built", "unbuildable", "refused"})
_RUN_MANIFEST_FILE: Final = "run_manifest.json"
_PROGRESS_FILE: Final = "progress.json"
#: The roles a staging tree is built from, by the name its index records them
#: under.
_TREE_SOURCE_ROLES: Final = (
    ("calibration_diagnostics", "calibrationDiagnostics"),
    ("build_manifest", "buildManifest"),
    ("release_manifest", "releaseManifest"),
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
    uploaded_source_files: int | None = None
    kept_source_files: int | None = None
    #: The build's copies left as stored because they hold other bytes.
    source_file_conflicts: list[str] | None = None
    #: The stored build a run's new tree replaces, or would replace.
    replaced_build_artifact_id: str | None = None
    #: What became of a staging run's copied files.
    run_files: RunFilesOutcome | None = None
    #: How many of the run's files were stored anew.
    copied_run_files: int | None = None
    run_files_reason: str | None = None
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


@dataclass(frozen=True)
class ReleasesRecordStep:
    """What a release run's first step did to the stored ``releases.json``.

    ``copied`` is ``copy_releases_record``'s answer when it gave one.
    ``skipped`` says why it was not called. ``error``: it raised, and the run
    is incomplete.
    """

    country: str
    outcome: ReleasesRecordStepOutcome
    copied: CopiedReleasesRecord | None = None
    skipped: Literal["dry_run"] | None = None
    #: Why the step was skipped, or what it raised.
    reason: str | None = None
    #: A later run should try it again.
    incomplete: bool = False

    def fields(self) -> dict[str, Any]:
        """The step as the summary's ``releases_record`` reports it."""

        document: dict[str, Any] = {"country": self.country, "outcome": self.outcome}
        if self.copied is not None:
            document.update(self.copied.result())
        if self.skipped is not None:
            document["skipped"] = self.skipped
        if self.reason is not None:
            document["reason"] = self.reason
        document["incomplete"] = self.incomplete
        return document

    def to_json(self) -> dict[str, Any]:
        return {"releases_record": self.fields()}


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
    #: A staging run's stored tree, if it has one.
    stored_build: Mapping[str, Any] | None = None
    #: The state of a staging run's folder this plan was made from.
    folder_fingerprint: str | None = None


@dataclass
class _StagingRun:
    """What the listing rules read of one staging run."""

    run_id: str
    folder: staging_run_files.RunFolder
    #: The run's row in a schema-1 ``runs.json``.
    index_row: Mapping[str, Any] | None = None
    run_manifest: Mapping[str, Any] | None = None
    progress: Mapping[str, Any] | None = None
    progress_read: bool = False
    structured: bool = False


@dataclass(frozen=True)
class _RunCopy:
    """What became of one run's copied files."""

    outcome: RunFilesOutcome
    files: int | None = None
    reason: str | None = None
    incomplete: bool = False


@dataclass
class _Reconciliation:
    country: str
    reader: HuggingFaceReader
    commands: TreeCommands
    submissions: SubmissionsInProgress
    follow_latest: FollowLatest
    copy_record: CopyReleasesRecord
    dry_run: bool
    clock: Callable[[], float]
    started: float
    time_limit_seconds: float
    #: Also cover staging runs older than ``JOB_COVERS_RUNS_FROM``.
    include_older_runs: bool = False
    #: Look again at trees recorded as finally refused for an unchanged folder.
    retry_refusals: bool = False
    listings: Listings = field(init=False)
    #: What became of each staging run's files in this execution.
    run_copies: dict[str, _RunCopy] = field(default_factory=dict)
    #: The settled tree results to record in the runs' copies.
    tree_checks: list[dict[str, Any]] = field(default_factory=list)
    #: Copied runs whose folders are gone from the staging repo.
    removed_run_ids: list[str] = field(default_factory=list)
    staging_commit: str | None = None

    def __post_init__(self) -> None:
        self.listings = Listings(self.reader)

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
            outcome = item if isinstance(item, ReconcileOutcome) else self.build(item)
            yield self.with_run_files(outcome)
            if (
                kind == "staging"
                and isinstance(item, _Planned)
                and outcome.outcome in _BUILD_ATTEMPTED
            ):
                yield from self.record_tree_checks()
        if kind == "staging":
            yield from self.record_tree_checks()

    def with_run_files(self, outcome: ReconcileOutcome) -> ReconcileOutcome:
        """``outcome`` with what became of its run's copied files."""

        copy = (
            self.run_copies.get(outcome.source_id)
            if outcome.kind == "staging" and outcome.source_id is not None
            else None
        )
        if copy is None:
            return outcome
        return replace(
            outcome,
            run_files=copy.outcome,
            copied_run_files=copy.files,
            run_files_reason=copy.reason,
            incomplete=outcome.incomplete or copy.incomplete,
        )

    def record_tree_checks(self) -> Iterator[ReconcileOutcome]:
        """Keep this execution's settled tree results in the runs' copies."""

        if self.dry_run or not (self.tree_checks or self.removed_run_ids):
            return
        repo = STAGING_REPOS[self.country]
        try:
            with TemporaryDirectory(prefix="staging-run-files-") as directory:
                staging_run_files.publish(
                    self.commands,
                    self.run_files_request(repo, [], self.tree_checks),
                    Path(directory),
                )
        except Exception as error:
            yield self.outcome(
                "staging",
                "error",
                hf_repo=repo,
                reason=(
                    f"The runs' tree results could not be recorded: {_described(error)}"
                ),
                incomplete=True,
            )
            return
        self.tree_checks.clear()
        self.removed_run_ids = []

    def run_files_request(
        self,
        repo: str,
        runs: Sequence[Mapping[str, Any]],
        tree_checks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "country": self.country,
            "hfRepo": repo,
            "hfCommitSha": self.staging_commit,
            "runs": list(runs),
            "treeChecks": list(tree_checks),
            "removedRunIds": [] if runs else self.removed_run_ids,
        }

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
                read = read_json(self.reader, repo, manifest_path, commit)
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
        """Copy every run's changed files, then give each run's final outcome
        and the plans of the trees to look at, the newest first, as the
        staging readers order runs."""

        repo = STAGING_REPOS[self.country]
        try:
            head = self.reader.head_commit(repo)
            listed = self.reader.list_files(repo, head)
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
        self.staging_commit = head
        try:
            copied_runs = staging_run_files.read_copied_runs(
                self.commands, self.country
            )
        except Exception as error:
            return [
                self.outcome(
                    "staging",
                    "error",
                    hf_repo=repo,
                    hf_commit=head,
                    reason=(
                        "The index of copied run files could not be read: "
                        f"{_described(error)}"
                    ),
                    incomplete=True,
                )
            ]
        folders = staging_run_files.run_folders(listed)
        self.removed_run_ids = sorted(
            run_id
            for run_id, record in copied_runs.items()
            if run_id not in folders and not record.get("removedAt")
        )
        # A schema-1 runs.json makes a folder without a run manifest a run,
        # and only such a folder needs it.
        index: dict[str, Mapping[str, Any]] | None = None
        index_failure: str | None = None
        if any(_RUN_MANIFEST_FILE not in folder.files for folder in folders.values()):
            try:
                index = self.run_index(repo, head)
            except Exception as error:
                index_failure = f"runs.json could not be read: {_described(error)}"
        finished: list[ReconcileOutcome] = []
        runs: list[_StagingRun] = []
        for run_id, folder in sorted(folders.items()):
            reported = {"source_id": run_id, "hf_repo": repo}
            if SAFE_IDENTIFIER.fullmatch(run_id) is None or run_id == "latest":
                finished.append(
                    self.outcome(
                        "staging",
                        "unbuildable",
                        hf_commit=head,
                        reason=f"Run folder {run_id} is not a run ID the tree worker takes.",
                        **reported,
                    )
                )
                continue
            if _RUN_MANIFEST_FILE not in folder.files:
                if index_failure is not None:
                    finished.append(
                        self.outcome(
                            "staging",
                            "error",
                            reason=index_failure,
                            incomplete=True,
                            **reported,
                        )
                    )
                    continue
                if index is None or (
                    _PROGRESS_FILE not in folder.files and run_id not in index
                ):
                    finished.append(
                        self.outcome(
                            "staging",
                            "not_a_run",
                            hf_commit=head,
                            reason=f"Folder {run_id} holds no run documents.",
                            **reported,
                        )
                    )
                    continue
            runs.append(
                _StagingRun(
                    run_id,
                    folder,
                    index_row=None if index is None else index.get(run_id),
                )
            )
        try:
            older_run_ids = self.runs_left_to_the_backfill(
                repo, head, runs, copied_runs
            )
        except Exception as error:
            return [
                *finished,
                self.outcome(
                    "staging",
                    "error",
                    hf_repo=repo,
                    hf_commit=head,
                    reason=(
                        "The run folders' last commits could not be read: "
                        f"{_described(error)}"
                    ),
                    incomplete=True,
                ),
            ]
        finished.extend(
            self.outcome(
                "staging",
                "older_run",
                source_id=run_id,
                hf_repo=repo,
                hf_commit=head,
                reason=(
                    "Its folder last changed before "
                    f"{JOB_COVERS_RUNS_FROM.date()}; the backfill covers it."
                ),
            )
            for run_id in sorted(older_run_ids)
        )
        runs = [run for run in runs if run.run_id not in older_run_ids]
        self.copy_run_files(repo, head, runs, copied_runs)
        planned: list[_Planned] = []
        for run in runs:
            build = stored.get(run.run_id)
            settled = self.settled_outcome(
                repo, head, run, build, copied_runs.get(run.run_id)
            )
            if settled is not None:
                finished.append(settled)
                continue
            result = self.staging_run(repo, head, run, build)
            if isinstance(result, _Planned):
                planned.append(result)
                continue
            finished.append(result)
            if result.outcome == "unbuildable":
                self.settle_tree(
                    run.run_id,
                    run.folder.fingerprint,
                    build.get("buildArtifactId") if build else None,
                    result,
                )
        planned.sort(key=lambda planned_build: planned_build.order, reverse=True)
        return [*finished, *planned]

    def runs_left_to_the_backfill(
        self,
        repo: str,
        head: str,
        runs: Sequence[_StagingRun],
        copied_runs: Mapping[str, Mapping[str, Any]],
    ) -> set[str]:
        """The runs without a copy whose folders last changed before the job's
        start. A folder with no last commit is covered, so that it is not lost."""

        uncopied = [run.run_id for run in runs if run.run_id not in copied_runs]
        if self.include_older_runs or not uncopied:
            return set()
        last_commits = self.reader.last_commits(repo, "runs", head)
        older: set[str] = set()
        for run_id in uncopied:
            last_commit = last_commits.get(f"runs/{run_id}")
            if (
                last_commit is not None
                and _instant(last_commit.date) < JOB_COVERS_RUNS_FROM
            ):
                older.add(run_id)
        return older

    def copy_run_files(
        self,
        repo: str,
        head: str,
        runs: Sequence[_StagingRun],
        copied_runs: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Store the copied files of each run whose folder changed."""

        changed: list[_StagingRun] = []
        for run in runs:
            record = copied_runs.get(run.run_id)
            if (
                record
                and not record.get("removedAt")
                and record.get("folderFingerprint") == run.folder.fingerprint
            ):
                self.run_copies[run.run_id] = _RunCopy("unchanged")
            elif self.dry_run:
                self.run_copies[run.run_id] = _RunCopy(
                    "would_copy", files=len(run.folder.copied_files)
                )
            else:
                changed.append(run)
        batch_size = staging_run_files.RUN_FILE_BATCH_SIZE
        for start in range(0, len(changed), batch_size):
            if self.clock() - self.started >= self.time_limit_seconds:
                for run in changed[start:]:
                    self.run_copies[run.run_id] = _RunCopy(
                        "deferred",
                        reason="No copy starts after the execution's time limit.",
                        incomplete=True,
                    )
                return
            self.copy_run_file_batch(repo, head, changed[start : start + batch_size])

    def copy_run_file_batch(
        self, repo: str, head: str, runs: Sequence[_StagingRun]
    ) -> None:
        with TemporaryDirectory(prefix="staging-run-files-") as directory:
            root = Path(directory)
            requested: list[dict[str, Any]] = []
            for number, run in enumerate(runs):
                try:
                    requested.append(self.downloaded_run(repo, head, run, root, number))
                except Exception as error:
                    self.run_copies[run.run_id] = _RunCopy(
                        "error", reason=_described(error), incomplete=True
                    )
            if not requested:
                return
            try:
                answer = staging_run_files.publish(
                    self.commands, self.run_files_request(repo, requested, []), root
                )
            except Exception as error:
                for run_request in requested:
                    self.run_copies[run_request["runId"]] = _RunCopy(
                        "error", reason=_described(error), incomplete=True
                    )
                return
        for run_request in requested:
            run_id = run_request["runId"]
            omitted = run_request["omittedFiles"]
            self.run_copies[run_id] = _RunCopy(
                "copied",
                files=answer.get(run_id, {}).get("uploaded"),
                reason=(
                    f"Too large to copy: {', '.join(omitted)}." if omitted else None
                ),
            )

    def downloaded_run(
        self, repo: str, head: str, run: _StagingRun, root: Path, number: int
    ) -> dict[str, Any]:
        """The publication request of one run, with its files on disk."""

        folder = root / str(number)
        folder.mkdir()
        files: list[dict[str, str]] = []
        omitted: list[str] = []
        for position, (name, listed) in enumerate(run.folder.copied_files.items()):
            if listed.size > MAX_COPIED_FILE_BYTES:
                omitted.append(name)
                continue
            destination = folder / str(position)
            self.reader.download_file(
                repo, listed, head, destination, max_bytes=MAX_COPIED_FILE_BYTES
            )
            files.append(
                {"name": name, "file": str(destination), "hfBlobId": listed.blob_id}
            )
        return {
            "runId": run.run_id,
            "folderFingerprint": run.folder.fingerprint,
            "indexRow": run.index_row,
            "files": files,
            "omittedFiles": omitted,
        }

    def settled_outcome(
        self,
        repo: str,
        head: str,
        run: _StagingRun,
        build: Mapping[str, Any] | None,
        record: Mapping[str, Any] | None,
    ) -> ReconcileOutcome | None:
        """The run's outcome when its tree needs no further look, else ``None``."""

        check = record.get("treeCheck") if record else None
        stored_id = build.get("buildArtifactId") if build else None
        if (
            not isinstance(check, Mapping)
            or check.get("folderFingerprint") != run.folder.fingerprint
            or check.get("buildArtifactId") != stored_id
        ):
            return None
        recorded = check.get("outcome")
        if recorded is not None:
            if self.retry_refusals:
                return None
            return self.outcome(
                "staging",
                recorded,
                source_id=run.run_id,
                hf_repo=repo,
                hf_commit=head,
                reason=check.get("reason"),
                source_file_conflicts=check.get("sourceFileConflicts"),
            )
        return (
            None if build is None else self.stored_run_outcome(repo, run.run_id, build)
        )

    def stored_run_outcome(
        self, repo: str, run_id: str, build: Mapping[str, Any]
    ) -> ReconcileOutcome:
        return self.outcome(
            "staging",
            "already_stored",
            source_id=run_id,
            hf_repo=build.get("hfRepo", repo),
            hf_commit=build.get("hfCommitSha"),
            created_at=build.get("createdAt"),
            label=build.get("label"),
            build_artifact_id=build.get("buildArtifactId"),
        )

    def read_run_manifest(self, repo: str, head: str, run: _StagingRun) -> None:
        """Read and classify a run's manifest; ``SourceUnbuildableError`` for one
        the staging readers refuse."""

        read = read_json(
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

    def run_index(self, repo: str, head: str) -> dict[str, Mapping[str, Any]] | None:
        """Each run's row in a schema-1 ``runs.json``; ``None`` without one.

        A ``runs.json`` that isn't JSON, or isn't a JSON object, is none, as
        the staging readers' ``stagingJsonOrNull`` and ``asObject`` take it.
        A failed read raises ``HuggingFaceReadError``.
        """

        try:
            read = read_json(self.reader, repo, "runs.json", head)
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
            row["run_id"]: row
            for row in rows
            if isinstance(row, dict) and _string(row.get("run_id"))
        }

    def progress(
        self, repo: str, head: str, run: _StagingRun
    ) -> Mapping[str, Any] | None:
        if not run.progress_read:
            read = read_json(
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
        build: Mapping[str, Any] | None,
    ) -> ReconcileOutcome | _Planned:
        run_id = run.run_id
        reported = {"source_id": run_id, "hf_repo": repo, "hf_commit": head}
        try:
            if _RUN_MANIFEST_FILE in run.folder.files:
                self.read_run_manifest(repo, head, run)
            progress = self.progress(repo, head, run)
            if (
                not run.structured
                and progress is not None
                and progress.get("run_id") not in (None, run_id)
            ):
                raise SourceUnbuildableError(
                    f"Run {run_id}'s progress names another run."
                )
            folder = f"staged/{run_id}"
            if self.submissions.in_progress(self.country, folder):
                return self.outcome(
                    "staging",
                    "submission_in_progress",
                    reason=f"A submission of {folder} is still being processed.",
                    **reported,
                )
            progress = progress or {}
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
            stored_build=build,
            folder_fingerprint=run.folder.fingerprint,
        )

    def tree_sources_differ(
        self, source: BuildSource, stored_build: Mapping[str, Any]
    ) -> bool:
        """Whether the run has a tree source its stored tree was not built from.

        A file only the stored tree has does not count: a submission's tree can
        hold manifests the run's folder lacks.
        """

        stored_sources = staging_run_files.read_tree_sources(
            self.commands, self.country, str(stored_build.get("buildArtifactId"))
        )
        for role, source_name in _TREE_SOURCE_ROLES:
            source_file = source.files.get(role)
            if source_file is None:
                continue
            recorded = stored_sources.get(source_name)
            recorded_sha256 = (
                recorded.get("sha256") if isinstance(recorded, Mapping) else None
            )
            sha256 = (
                source_file.declared_sha256
                or source_file.listed.lfs_sha256
                or hashlib.sha256(
                    self.reader.read_file(
                        source_file.dataset,
                        source_file.listed.path,
                        source_file.revision,
                    )
                ).hexdigest()
            )
            if sha256 != recorded_sha256:
                return True
        return False

    def settle_tree(
        self,
        run_id: str,
        folder_fingerprint: str | None,
        build_artifact_id: str | None,
        final_outcome: ReconcileOutcome | None = None,
    ) -> None:
        """Remember that this state of the run's folder needs no further look,
        with the outcome to repeat when it ended without a new tree."""

        if self.dry_run or folder_fingerprint is None:
            return
        check: dict[str, Any] = {
            "runId": run_id,
            "folderFingerprint": folder_fingerprint,
            "buildArtifactId": build_artifact_id,
        }
        if final_outcome is not None:
            check["outcome"] = final_outcome.outcome
            check["reason"] = final_outcome.reason
            if final_outcome.source_file_conflicts:
                check["sourceFileConflicts"] = final_outcome.source_file_conflicts
        self.tree_checks.append(check)

    def settle_plan(
        self, planned: _Planned, final_outcome: ReconcileOutcome
    ) -> ReconcileOutcome:
        """Remember a planned run's final outcome, and return it."""

        stored = planned.stored_build
        self.settle_tree(
            planned.entry.source_id,
            planned.folder_fingerprint,
            stored.get("buildArtifactId") if stored else None,
            final_outcome,
        )
        return final_outcome

    # The stored releases.json -------------------------------------------------

    def releases_record_step(self) -> ReleasesRecordStep:
        """Store the country's releases.json at the release repo's head."""

        if self.dry_run:
            return ReleasesRecordStep(
                country=self.country,
                outcome="skipped",
                skipped="dry_run",
                reason=f"A dry run does not store {RELEASES_RECORD_PATH}.",
            )
        try:
            copied = self.copy_record(self.country)
        except Exception as error:
            return ReleasesRecordStep(
                country=self.country,
                outcome="error",
                reason=_described(error),
                incomplete=True,
            )
        return ReleasesRecordStep(
            country=self.country, outcome=copied.outcome, copied=copied
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
                release_source(entry, self.listings)
                if kind == "release"
                else staging_source(entry, self.reader, self.listings)
            )
        except SourceWithoutDiagnosticsError as error:
            if planned.stored_build is not None:
                self.settle_tree(
                    entry.source_id,
                    planned.folder_fingerprint,
                    planned.stored_build.get("buildArtifactId"),
                )
                return self.stored_run_outcome(
                    entry.hf_repo, entry.source_id, planned.stored_build
                )
            return self.settle_plan(
                planned,
                outcome(
                    "ineligible",
                    reason=shortened_message(
                        f"Staging run has no calibration diagnostics: {error}"
                    ),
                ),
            )
        except SourceUnbuildableError as error:
            return self.settle_plan(
                planned, outcome("unbuildable", reason=shortened_message(str(error)))
            )
        except Exception as error:
            return outcome("error", reason=_described(error), incomplete=True)
        replaced: str | None = None
        if planned.stored_build is not None:
            try:
                differs = self.tree_sources_differ(source, planned.stored_build)
            except Exception as error:
                return outcome("error", reason=_described(error), incomplete=True)
            stored_id = planned.stored_build.get("buildArtifactId")
            if not differs:
                self.settle_tree(entry.source_id, planned.folder_fingerprint, stored_id)
                return self.stored_run_outcome(
                    entry.hf_repo, entry.source_id, planned.stored_build
                )
            replaced = stored_id
        limit = backfill.MAX_SOURCE_FILE_BYTES
        oversized = [
            source_file.listed
            for source_file in source.files.values()
            if source_file.listed.size > limit
        ]
        if oversized:
            return self.settle_plan(
                planned,
                outcome(
                    "too_large",
                    reason=(
                        f"{oversized[0].path} is {oversized[0].size} bytes, over the "
                        f"{limit}-byte limit."
                    ),
                ),
            )
        if self.dry_run:
            return outcome("would_build", replaced_build_artifact_id=replaced)
        try:
            published = build_and_publish_source(
                source,
                self.reader,
                self.commands,
                replace_staged_run=replaced is not None,
                reuse_stored_tree=kind == "staging",
            )
        except (SourceUnbuildableError, TreeUnbuildableError) as error:
            return self.settle_plan(
                planned, outcome("unbuildable", reason=shortened_message(str(error)))
            )
        except TreeAlreadyStoredError as error:
            if replaced is not None:
                # The tree found is the one that was to be replaced.
                return self.settle_plan(
                    planned,
                    outcome(
                        "refused",
                        reason=shortened_message(
                            f"Build {error.submitted_build_artifact_id} could not "
                            f"replace stored build {error.build_artifact_id}: {error}"
                        ),
                        source_file_conflicts=list(error.source_file_conflicts),
                    ),
                )
            # A submission (or another writer) stored a tree for this build
            # while it was being built.
            return outcome(
                "already_stored",
                reason=(
                    "The store gained a tree for this build while it was built "
                    f"(build {error.build_artifact_id}), so its publication was "
                    "refused."
                ),
                build_artifact_id=error.build_artifact_id,
                source_file_conflicts=list(error.source_file_conflicts),
            )
        except TreeRefusedError as error:
            return self.settle_plan(
                planned,
                outcome(
                    "refused",
                    reason=shortened_message(str(error)),
                    source_file_conflicts=list(error.source_file_conflicts),
                ),
            )
        except Exception as error:
            return outcome("error", reason=_described(error), incomplete=True)
        self.settle_tree(
            entry.source_id, planned.folder_fingerprint, published.build_artifact_id
        )
        built = outcome(
            "built",
            replaced_build_artifact_id=replaced,
            build_artifact_id=published.build_artifact_id,
            uploaded_parts=published.uploaded_parts,
            kept_parts=published.kept_parts,
            uploaded_source_files=published.uploaded_source_files,
            kept_source_files=published.kept_source_files,
            source_file_conflicts=list(published.source_file_conflicts),
        )
        restored = published.restored_tree
        if restored is None:
            return built
        return replace(
            built,
            hf_commit=restored.get("hfCommitSha"),
            created_at=restored.get("createdAt"),
            label=restored.get("label"),
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
    copy_record: CopyReleasesRecord,
    dry_run: bool = False,
    clock: Callable[[], float] = time.monotonic,
    time_limit_seconds: float = BUILD_TIME_LIMIT_SECONDS,
    include_older_runs: bool = False,
    retry_refusals: bool | None = None,
) -> Iterator[ReconcileOutcome | LatestStep | ReleasesRecordStep]:
    """Reconcile ``country``'s ``kinds``, in that order, yielding each outcome.

    ``include_older_runs`` also covers staging runs older than
    ``JOB_COVERS_RUNS_FROM``. ``retry_refusals`` looks again at trees recorded
    as finally refused while their folder is unchanged; by default it follows
    ``include_older_runs``, as ``--include-older-runs`` asks. The backfill's
    staging step passes ``False``, so its retries don't build them again.

    The release kind starts with its ``ReleasesRecordStep``: ``copy_record``
    for the country, called before the release lock is waited for. It ends
    with its ``LatestStep``: ``follow_latest`` for the country, called after
    the release builds and inside the release lock.

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
        copy_record=copy_record,
        dry_run=dry_run,
        clock=clock,
        started=clock(),
        time_limit_seconds=time_limit_seconds,
        include_older_runs=include_older_runs,
        retry_refusals=(
            include_older_runs if retry_refusals is None else retry_refusals
        ),
    )
    for kind in kinds:
        if kind == "release":
            yield reconciliation.releases_record_step()
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
    outcomes: Iterable[ReconcileOutcome | LatestStep | ReleasesRecordStep],
    *,
    country: str,
    kinds: Sequence[str],
    request_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """The run's summary: its counts, and whether it is incomplete.

    ``outcomes`` counts every ``ReconcileOutcome``: each build's, and each
    outcome for a whole kind (``no_releases_record``,
    ``releases_record_refused``, ``lock_wait_expired``, or an ``error`` when
    a repo or the store's manifest could not be read).
    ``incomplete_sources`` lists those that are incomplete, a whole kind's
    with ``source_id`` ``None``. ``latest`` is the release run's latest step
    (``LatestStep.fields``), or ``None`` for a run without releases or one
    that stopped before the step. ``releases_record`` is the release run's
    step that stores ``releases.json`` (``ReleasesRecordStep.fields``), or
    ``None`` for a run without releases. Neither step is counted in
    ``outcomes``. When one is incomplete, so is the run, but it adds no entry
    to ``incomplete_sources``. ``run_files``, present for a run that looked at
    staging runs, counts what became of their copied files.
    """

    listed = list(outcomes)
    builds = [outcome for outcome in listed if isinstance(outcome, ReconcileOutcome)]
    latest_steps = [outcome for outcome in listed if isinstance(outcome, LatestStep)]
    record_steps = [
        outcome for outcome in listed if isinstance(outcome, ReleasesRecordStep)
    ]
    run_files = Counter(
        outcome.run_files for outcome in builds if outcome.run_files is not None
    )
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
        "releases_record": record_steps[-1].fields() if record_steps else None,
        **({"run_files": dict(run_files)} if run_files else {}),
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
