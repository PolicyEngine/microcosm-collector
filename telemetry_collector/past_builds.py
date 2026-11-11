"""An inventory of past Microcosm builds, made on demand and never stored.

Past builds come from two places: the folders staged under ``staged/<run_id>/``
in each country's Hugging Face release dataset (``RELEASE_REPOS``), and the
collector's recorded runs (``telemetry_runs``). Each build is classed by the
Microcosm commit that made it. Its folder's ``staged_manifest.json`` records it
as ``summary.git_commit``, which Microcosm copies from the build manifest's
``git rev-parse HEAD``. The build's emitter (the microcosm-emitter package)
sends the same value, from Microcosm's ``runtime_identity()``, in the run's
``run`` event as ``details.identity.git_commit``, which is used when no staged
manifest records one.

GitHub's compare API says whether that commit contains the merge commits of
microcosm#1144, which made builds record execution evidence, and of
microcosm#1151. Each build also says which graph files its folder holds, and so
what its Orrery graph document can be rebuilt from on Hugging Face. Builds made
before #1144, and builds made before #1151 with nothing on Hugging Face to
rebuild from, are listed but marked skipped. GitHub allows few requests
without a token, so the builds that can be rebuilt from Hugging Face are classed
first.

``list_past_builds`` returns the inventory; the ``past-builds`` operator
command prints it as JSON.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final, Literal

import httpx
from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from telemetry_collector.calibration_trees import RELEASE_REPOS
from telemetry_collector.database import TelemetryEvent, TelemetryRun
from telemetry_collector.huggingface_read import HuggingFaceReader

CommitClass = Literal["before_1144", "between_1144_and_1151", "after_1151", "unknown"]
Rebuild = Literal["full", "declaration_only", "none"]
CommitSource = Literal["staged_manifest", "telemetry"]

COMMIT_CLASSES: Final[tuple[CommitClass, ...]] = (
    "after_1151",
    "between_1144_and_1151",
    "before_1144",
    "unknown",
)
REBUILDS: Final[tuple[Rebuild, ...]] = ("full", "declaration_only", "none")

MICROCOSM_REPO: Final = "PolicyEngine/microcosm"
#: The merge commit of microcosm#1144, from which builds record execution evidence.
EXECUTION_EVIDENCE_MERGE: Final = "58b4c46175731cac940b897a42d85cd893cb3381"
#: The merge commit of microcosm#1151.
GRAPH_SCHEMA_MERGE: Final = "852ec44e6bc75db8399ebe3f649b2987c3d77424"
GITHUB_API: Final = "https://api.github.com"
GITHUB_TIMEOUT_SECONDS: Final = 30.0
GITHUB_HEADERS: Final = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "microcosm-collector past-builds inventory",
}

STAGED_PREFIX: Final = "staged"
STAGED_MANIFEST: Final = "staged_manifest.json"
GRAPH_SCHEMA: Final = "graph.schema.json"
EXECUTION_OVERLAY: Final = "graph.execution.json"
#: The graph files a staged folder may hold, in the order the inventory lists them.
GRAPH_FILES: Final = (
    GRAPH_SCHEMA,
    EXECUTION_OVERLAY,
    "graph.orrery.json",
    "execution.evidence.json",
    "orrery.publication.json",
)

SKIPPED_BEFORE_1144: Final = "made before microcosm#1144"
SKIPPED_BEFORE_1151: Final = (
    "made before microcosm#1151 with nothing on Hugging Face to rebuild from"
)
_COMMIT: Final = re.compile(r"^[0-9a-f]{40}$")
#: Microcosm registers UK runs as GB; the release datasets call the country uk.
_RELEASE_COUNTRY_BY_RECORDED_CODE: Final = {"GB": "uk"}


@dataclass(frozen=True)
class RecordedRun:
    """A run the collector has a telemetry record of."""

    run_id: str
    country_code: str
    #: The first non-null ``details.identity.git_commit`` of the run's ``run`` events.
    microcosm_commit: str | None


@dataclass(frozen=True)
class CommitClassification:
    commit_class: CommitClass
    #: Why the class is ``unknown``; ``None`` otherwise.
    reason: str | None = None


@dataclass(frozen=True)
class PastBuild:
    run_id: str
    country_code: str
    #: The release dataset and revision the staged folder was read at.
    dataset: str | None
    revision: str | None
    staged_folder: str | None
    recorded_run: bool
    microcosm_commit: str | None
    commit_source: CommitSource | None
    commit_class: CommitClass
    class_reason: str | None
    graph_files: tuple[str, ...]
    rebuild: Rebuild
    skipped: bool
    skip_reason: str | None


@dataclass(frozen=True)
class PastBuildInventory:
    created_at: str
    dataset_revisions: dict[str, str]
    builds: tuple[PastBuild, ...]
    counts: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class GitHubUnavailableError(RuntimeError):
    """GitHub gave no usable answer; later commits are not checked either."""


class CommitNotFoundError(Exception):
    """GitHub has no such commit in Microcosm's repo; other commits are checked."""


class GitHubCommitClassifier:
    """Classes Microcosm commits with GitHub's compare API, without a token.

    Each distinct commit is checked once: against #1151 first, and against
    #1144 only when it doesn't contain #1151. After a rate-limit answer or any
    other failure, that commit and every later one are ``unknown``.
    """

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=GITHUB_TIMEOUT_SECONDS)
        self._owns_client = client is None
        self._classifications: dict[str, CommitClassification] = {}
        self._stopped_reason: str | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubCommitClassifier:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __call__(self, commit: str) -> CommitClassification:
        if not _COMMIT.fullmatch(commit):
            return CommitClassification(
                "unknown",
                "the recorded Microcosm commit is not a 40-character commit hash",
            )
        if commit in self._classifications:
            return self._classifications[commit]
        if self._stopped_reason is not None:
            return CommitClassification("unknown", self._stopped_reason)
        try:
            if self._contains(commit, GRAPH_SCHEMA_MERGE):
                classification = CommitClassification("after_1151")
            elif self._contains(commit, EXECUTION_EVIDENCE_MERGE):
                classification = CommitClassification("between_1144_and_1151")
            else:
                classification = CommitClassification("before_1144")
        except CommitNotFoundError:
            classification = CommitClassification(
                "unknown", f"GitHub has no commit {commit} in {MICROCOSM_REPO}"
            )
        except GitHubUnavailableError as error:
            self._stopped_reason = str(error)
            return CommitClassification("unknown", self._stopped_reason)
        self._classifications[commit] = classification
        return classification

    def _contains(self, commit: str, merge_commit: str) -> bool:
        """Whether ``commit`` has ``merge_commit`` as an ancestor (or is it).

        It compares ``<commit>...<merge_commit>``: when the commit contains the
        merge commit, the merge commit is ``behind`` (or ``identical``), and
        GitHub's answer lists no commits or files.
        """

        url = f"{GITHUB_API}/repos/{MICROCOSM_REPO}/compare/{commit}...{merge_commit}"
        try:
            response = self._client.get(
                url,
                params={"per_page": "1"},
                headers=GITHUB_HEADERS,
                timeout=GITHUB_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise GitHubUnavailableError(
                "GitHub could not be reached to class the commit"
            ) from error
        if response.status_code == 404:
            raise CommitNotFoundError(commit)
        if response.status_code in {403, 429} and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        ):
            raise GitHubUnavailableError(
                "GitHub's rate limit for requests without a token was reached"
            )
        if response.status_code != 200:
            raise GitHubUnavailableError(
                f"GitHub answered HTTP {response.status_code} to the commit comparison"
            )
        try:
            comparison = response.json()["status"]
        except (ValueError, KeyError, TypeError):
            comparison = None
        if comparison in {"behind", "identical"}:
            return True
        if comparison in {"ahead", "diverged"}:
            return False
        raise GitHubUnavailableError("GitHub returned an unusable commit comparison")


def recorded_runs(engine: Engine) -> list[RecordedRun]:
    """Every run in ``telemetry_runs``, with the commit its ``run`` events record.

    The commit is the first non-null ``details.identity.git_commit`` among the
    run's ``run`` events, in the order they were emitted. The country is the
    release datasets' code (``GB`` becomes ``uk``).
    """

    commit_column = TelemetryEvent.details[("identity", "git_commit")].astext
    query = (
        select(TelemetryRun.run_id, TelemetryRun.country_code, commit_column)
        .outerjoin(
            TelemetryEvent,
            and_(
                TelemetryEvent.run_id == TelemetryRun.run_id,
                TelemetryEvent.event_type == "run",
            ),
        )
        .order_by(
            TelemetryRun.run_id, TelemetryEvent.emitted_at, TelemetryEvent.sequence
        )
    )
    runs: dict[str, RecordedRun] = {}
    with engine.connect() as connection:
        for run_id, country_code, commit in connection.execute(query):
            known = runs.get(run_id)
            if known is not None and known.microcosm_commit is not None:
                continue
            runs[run_id] = RecordedRun(
                run_id=run_id,
                country_code=_RELEASE_COUNTRY_BY_RECORDED_CODE.get(
                    country_code, country_code.lower()
                ),
                microcosm_commit=commit,
            )
    return list(runs.values())


@dataclass(frozen=True)
class _StagedFolder:
    run_id: str
    country_code: str
    dataset: str
    revision: str
    file_names: frozenset[str]


def _staged_folders(
    reader: HuggingFaceReader, country_code: str, dataset: str, revision: str
) -> list[_StagedFolder]:
    file_names_by_run: dict[str, set[str]] = {}
    for dataset_file in reader.list_files(dataset, revision):
        parts = dataset_file.path.split("/")
        if len(parts) == 3 and parts[0] == STAGED_PREFIX:
            file_names_by_run.setdefault(parts[1], set()).add(parts[2])
    return [
        _StagedFolder(run_id, country_code, dataset, revision, frozenset(file_names))
        for run_id, file_names in file_names_by_run.items()
    ]


def _staged_commit(
    reader: HuggingFaceReader, folder: _StagedFolder
) -> tuple[str | None, str | None]:
    """The commit a folder's staged manifest records, or why there is none."""

    if STAGED_MANIFEST not in folder.file_names:
        return None, f"the staged folder has no {STAGED_MANIFEST}"
    content = reader.read_file(
        folder.dataset,
        f"{STAGED_PREFIX}/{folder.run_id}/{STAGED_MANIFEST}",
        folder.revision,
    )
    try:
        manifest = json.loads(content)
    except ValueError:
        return None, f"{STAGED_MANIFEST} is not JSON"
    try:
        commit = manifest["summary"]["git_commit"]
    except (KeyError, TypeError):
        commit = None
    if not isinstance(commit, str):
        return None, f"{STAGED_MANIFEST} records no summary.git_commit"
    return commit, None


@dataclass(frozen=True)
class _FoundBuild:
    """A build's staged folder or recorded run, or both, before it is classed."""

    run_id: str
    country_code: str
    folder: _StagedFolder | None
    recorded_run: bool
    commit: str | None
    commit_source: CommitSource | None
    #: Why there is no commit; ``None`` when there is one.
    commit_reason: str | None
    file_names: frozenset[str]
    rebuild: Rebuild


def _found_build(
    reader: HuggingFaceReader,
    run_id: str,
    folder: _StagedFolder | None,
    recorded_run: RecordedRun | None,
) -> _FoundBuild:
    commit: str | None = None
    commit_reason: str | None = "the build has no staged folder to read its commit from"
    file_names: frozenset[str] = frozenset()
    if folder is not None:
        commit, commit_reason = _staged_commit(reader, folder)
        file_names = folder.file_names
    commit_source: CommitSource | None = (
        "staged_manifest" if commit is not None else None
    )
    if commit is None and recorded_run is not None:
        if recorded_run.microcosm_commit is not None:
            commit = recorded_run.microcosm_commit
            commit_source = "telemetry"
            commit_reason = None
        else:
            commit_reason = f"{commit_reason}, and its telemetry records none"
    if folder is not None:
        country_code = folder.country_code
    else:
        assert recorded_run is not None
        country_code = recorded_run.country_code
    return _FoundBuild(
        run_id=run_id,
        country_code=country_code,
        folder=folder,
        recorded_run=recorded_run is not None,
        commit=commit,
        commit_source=commit_source,
        commit_reason=commit_reason,
        file_names=file_names,
        rebuild=_rebuild(file_names),
    )


def _classing_order(found: _FoundBuild) -> int:
    """Staged builds that can be rebuilt, then other staged builds, then
    recorded runs without a staged folder."""

    if found.folder is None:
        return 2
    return 0 if found.rebuild != "none" else 1


def _classify(
    found_builds: Iterable[_FoundBuild],
    classifier: Callable[[str], CommitClassification],
) -> dict[str, CommitClassification]:
    """Class each distinct commit, the builds that can be rebuilt first.

    GitHub's hourly allowance for requests without a token may run out, and
    every commit after that is ``unknown``. So commits are checked in
    ``_classing_order``, and within each group in descending run-ID order,
    which puts newer builds first among run IDs that share a prefix, such as
    ``uk-frs-calibration-attempt-<UTC time>-<suffix>``.
    """

    newest_first = sorted(found_builds, key=lambda found: found.run_id, reverse=True)
    classifications: dict[str, CommitClassification] = {}
    for found in sorted(newest_first, key=_classing_order):
        if found.commit is not None and found.commit not in classifications:
            classifications[found.commit] = classifier(found.commit)
    return classifications


def _rebuild(file_names: frozenset[str]) -> Rebuild:
    if GRAPH_SCHEMA not in file_names:
        return "none"
    return "full" if EXECUTION_OVERLAY in file_names else "declaration_only"


def _skip_reason(commit_class: CommitClass, rebuild: Rebuild) -> str | None:
    if commit_class == "before_1144":
        return SKIPPED_BEFORE_1144
    if commit_class == "between_1144_and_1151" and rebuild == "none":
        return SKIPPED_BEFORE_1151
    return None


def list_past_builds(
    reader: HuggingFaceReader,
    runs: Iterable[RecordedRun],
    classifier: Callable[[str], CommitClassification],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    release_datasets: Mapping[str, str] = RELEASE_REPOS,
) -> PastBuildInventory:
    """List every staged folder and recorded run as one past build each."""

    created_at = clock().isoformat()
    dataset_revisions: dict[str, str] = {}
    folders: dict[str, _StagedFolder] = {}
    for country_code, dataset in release_datasets.items():
        revision = reader.head_commit(dataset)
        dataset_revisions[dataset] = revision
        for folder in _staged_folders(reader, country_code, dataset, revision):
            folders.setdefault(folder.run_id, folder)
    recorded = {run.run_id: run for run in runs}

    found_builds = [
        _found_build(reader, run_id, folders.get(run_id), recorded.get(run_id))
        for run_id in sorted(folders.keys() | recorded.keys())
    ]
    classifications = _classify(found_builds, classifier)

    builds: list[PastBuild] = []
    for found in found_builds:
        classification = (
            classifications[found.commit]
            if found.commit is not None
            else CommitClassification("unknown", found.commit_reason)
        )
        skip_reason = _skip_reason(classification.commit_class, found.rebuild)
        folder = found.folder
        builds.append(
            PastBuild(
                run_id=found.run_id,
                country_code=found.country_code,
                dataset=folder.dataset if folder is not None else None,
                revision=folder.revision if folder is not None else None,
                staged_folder=(
                    f"{STAGED_PREFIX}/{found.run_id}" if folder is not None else None
                ),
                recorded_run=found.recorded_run,
                microcosm_commit=found.commit,
                commit_source=found.commit_source,
                commit_class=classification.commit_class,
                class_reason=classification.reason,
                graph_files=tuple(
                    name for name in GRAPH_FILES if name in found.file_names
                ),
                rebuild=found.rebuild,
                skipped=skip_reason is not None,
                skip_reason=skip_reason,
            )
        )

    counts = {
        "commit_class": {
            commit_class: sum(build.commit_class == commit_class for build in builds)
            for commit_class in COMMIT_CLASSES
        },
        "rebuild": {
            rebuild: sum(build.rebuild == rebuild for build in builds)
            for rebuild in REBUILDS
        },
        "skipped": sum(build.skipped for build in builds),
    }
    return PastBuildInventory(
        created_at=created_at,
        dataset_revisions=dataset_revisions,
        builds=tuple(builds),
        counts=counts,
    )
