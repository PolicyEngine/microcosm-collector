"""Project past Microcosm builds' staged graph inputs again, as new builds' are.

Past builds never sent ``orrery-inputs`` submissions. For each build in the
inventory of past builds (``telemetry_collector.past_builds``) made after
microcosm#1151 whose staged folder holds a graph schema and an execution
overlay, the ``reproject-past-builds`` operator command makes the submission
the build would have sent, owned by the collector (``COLLECTOR_OWNER``):

- **Files:** the folder's ``graph.schema.json`` (``schema``),
  ``graph.execution.json`` (``execution_overlay``) and every
  ``upstream-<sha256>.json`` (``upstream``), read at the inventory's revision
  of the dataset. Evidence files are left out: the collector never publishes
  them.
- **Provenance:** the build's run ID as its run and build ID, its country as
  Microcosm registers it (``GB`` for ``uk``), no release, and the Hugging Face
  folder at the newest last commit among the submitted files. The submission
  ID is the hash of the request (``derive_submission_id``), so another run
  makes the same submission unless a submitted file changed.
- **Licensed data:** true unless the build was staged in a public dataset
  (``HUGGINGFACE_DATASETS``), so a private dataset's documents are private.

Before anything is downloaded, each build is checked from Hugging Face's
listings alone (``_check``), in a dry run too: the submission rules (with each
file's listed size), the Orrery handler's input limit for the schema and the
overlay, and a last commit for every submitted file. Each download must then
match its listing: its size, and its SHA-256 for a file stored with LFS or its
git blob ID otherwise.

Each file is created create-only in the intake bucket under the submission's
ID, as an upload would be, and the submission is accepted as completion
accepts one (``storage_problems``, ``stored_generations``). Its job then runs
at once through the worker's ``run_job`` and the registered handler, so the
document is checked, validated, published and recorded as a new build's is. A
job found ``processing`` is run too: the same lease holder (a task retry in
the same Cloud Run execution) continues it, and another holder's live lease
skips it. A job released for a retry stays queued, and the scheduled recovery
dispatches it to the worker job.

Outcomes are produced one build at a time. An error in one build is that
build's ``error`` outcome, and the next build is still projected. Builds that
can't be projected are ``left_out`` with the reason. An outcome is
``incomplete`` when a later run should try the build again: an ``error``, or
a build left out because GitHub stopped answering before it was classed.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import ValidationError

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.google_cloud import StoredObjectConflictError
from telemetry_collector.huggingface_read import (
    HUGGINGFACE_DATASETS,
    DatasetFile,
    HuggingFaceReader,
)
from telemetry_collector.intake import (
    IntakeObjectStore,
    storage_problems,
    stored_generations,
    stored_object_mismatch,
)
from telemetry_collector.orrery_graphs import MAX_INPUT_BYTES
from telemetry_collector.past_builds import (
    EXECUTION_OVERLAY,
    GRAPH_SCHEMA,
    PastBuild,
    PastBuildInventory,
)
from telemetry_collector.submission_store import SubmissionRecord, SubmissionStore
from telemetry_collector.submissions import (
    CONTRACT_VERSION,
    UPSTREAM_FILE_NAME_PATTERN,
    SubmissionRequest,
    intake_object_name,
    intake_object_prefix,
)
from telemetry_collector.worker import JobHandler, JobOutcome, run_job

ProjectionOutcomeKind = Literal[
    "left_out",
    "would_reproject",
    "refused",
    "error",
    "queued",
    "processing",
    "succeeded",
    "rejected",
    "failed",
]

#: The owner of the submissions the collector makes for past builds. No
#: Hugging Face user has this ID, so no build can complete or read them.
COLLECTOR_OWNER: Final = HuggingFacePrincipal(
    user_id="microcosm-collector", username="microcosm-collector", organizations=()
)
SUBMISSION_KIND: Final = "orrery-inputs"
#: The Orrery handler reads the schema and the overlay only up to this size.
HANDLER_INPUT_ROLES: Final = frozenset({"schema", "execution_overlay"})
#: Microcosm registers UK runs as GB; the release datasets call the country uk.
_SUBMISSION_COUNTRY_BY_RELEASE_COUNTRY: Final = {"uk": "GB"}
_PUBLIC_DATASETS: Final = frozenset(
    dataset.name for dataset in HUGGINGFACE_DATASETS if dataset.public
)
_UPSTREAM_FILE_NAME: Final = re.compile(UPSTREAM_FILE_NAME_PATTERN)
#: Stands in for each file's SHA-256 while the listing alone is checked.
_UNREAD_SHA256: Final = "0" * 64
_LEFT_OUT_WITHOUT_OVERLAY: Final = (
    "its staged folder holds the schema without the execution overlay"
)
_LEFT_OUT_WITHOUT_INPUTS: Final = "it has nothing on Hugging Face to project"
_LEFT_OUT_WITHOUT_COMMIT: Final = (
    "Hugging Face lists no last commit for one of its submitted files"
)
_RELEASED_FOR_RETRY: Final = (
    "its job was released for a retry; the scheduled recovery dispatches it to "
    "the worker job"
)
_NOT_CLAIMED: Final = (
    "its job could not be claimed; the scheduled recovery dispatches it to the "
    "worker job"
)
_LEASED_ELSEWHERE: Final = (
    "another holder's lease on its job is live; the scheduled recovery queues "
    "the job again once the lease expires"
)


class IntakeObjectWriter(IntakeObjectStore, Protocol):
    """The intake bucket, which the command also creates objects in."""

    def create_object(self, object_name: str, source: Path, *, sha256: str) -> bool:
        """Create the object once; False when an equal one is kept."""


@dataclass(frozen=True)
class ProjectionOutcome:
    """What happened to one past build."""

    run_id: str
    #: ``left_out``, ``would_reproject``, ``refused`` or ``error``, or else the
    #: submission's status: ``succeeded``, ``rejected``, ``failed``, ``queued``
    #: or ``processing``.
    outcome: ProjectionOutcomeKind
    reason: str | None = None
    submission_id: str | None = None
    #: Whether this run created the submission; false when it already existed.
    new_submission: bool = False
    #: The handler's result, for a submission that succeeded.
    result: dict[str, Any] | None = None
    #: True when a later run should try this build again.
    incomplete: bool = False


def left_out_reason(build: PastBuild) -> str | None:
    """Why a past build isn't projected, or ``None`` when it is."""

    if build.skipped:
        return build.skip_reason
    if build.commit_class != "after_1151":
        return build.class_reason or (
            f"its Microcosm commit is classed {build.commit_class}"
        )
    if build.rebuild == "declaration_only":
        return _LEFT_OUT_WITHOUT_OVERLAY
    if (
        build.rebuild != "full"
        or build.staged_folder is None
        or build.dataset is None
        or build.revision is None
    ):
        return _LEFT_OUT_WITHOUT_INPUTS
    return None


@dataclass(frozen=True)
class _SubmittedFile:
    name: str
    role: str
    size: int
    sha256: str


def _submission_request(
    build: PastBuild, inputs_commit: str, files: list[_SubmittedFile]
) -> SubmissionRequest:
    assert build.dataset is not None and build.staged_folder is not None
    country_code = _SUBMISSION_COUNTRY_BY_RELEASE_COUNTRY.get(
        build.country_code, build.country_code.upper()
    )
    return SubmissionRequest.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "kind": SUBMISSION_KIND,
            "licensed_data": build.dataset not in _PUBLIC_DATASETS,
            "provenance": {
                "run_id": build.run_id,
                "build_id": build.run_id,
                "country_code": country_code,
                "release": None,
                "huggingface": {
                    "repo": build.dataset,
                    "commit": inputs_commit,
                    "folder": build.staged_folder,
                },
            },
            "files": [
                {
                    "name": file.name,
                    "role": file.role,
                    "size": file.size,
                    "sha256": file.sha256,
                }
                for file in files
            ],
        }
    )


def _refusal(error: ValidationError) -> str:
    return "; ".join(detail["msg"] for detail in error.errors())


@dataclass(frozen=True)
class _Checked:
    """A build that passed every check made from Hugging Face's listings."""

    role_files: list[tuple[str, DatasetFile]]
    inputs_commit: str


def _check(
    build: PastBuild,
    reader: HuggingFaceReader,
    files_by_folder: Mapping[str, Mapping[str, DatasetFile]],
) -> _Checked | ProjectionOutcome:
    """Check a build from the listings alone, before anything is downloaded."""

    assert build.dataset is not None and build.staged_folder is not None
    assert build.revision is not None
    listed = files_by_folder.get(build.staged_folder, {})
    role_files = []
    for name, role in (
        (GRAPH_SCHEMA, "schema"),
        (EXECUTION_OVERLAY, "execution_overlay"),
    ):
        if name not in listed:
            raise RuntimeError(f"The staged folder no longer lists {name}.")
        role_files.append((role, listed[name]))
    role_files += [
        ("upstream", listed[name])
        for name in sorted(listed)
        if _UPSTREAM_FILE_NAME.fullmatch(name)
    ]
    for role, listed_file in role_files:
        if role in HANDLER_INPUT_ROLES and listed_file.size > MAX_INPUT_BYTES:
            name = listed_file.path.rpartition("/")[2]
            return ProjectionOutcome(
                run_id=build.run_id,
                outcome="refused",
                reason=(
                    f"{name} ({role}) is {listed_file.size} bytes, over the "
                    f"Orrery handler's input limit of {MAX_INPUT_BYTES} bytes"
                ),
            )
    try:
        _submission_request(
            build,
            "0" * 40,
            [
                _SubmittedFile(
                    name=listed_file.path.rpartition("/")[2],
                    role=role,
                    size=listed_file.size,
                    sha256=_UNREAD_SHA256,
                )
                for role, listed_file in role_files
            ],
        )
    except ValidationError as error:
        return ProjectionOutcome(
            run_id=build.run_id, outcome="refused", reason=_refusal(error)
        )
    last_commits = reader.last_commits(
        build.dataset, build.staged_folder, build.revision
    )
    paths = [listed_file.path for _, listed_file in role_files]
    if any(path not in last_commits for path in paths):
        return ProjectionOutcome(
            run_id=build.run_id, outcome="left_out", reason=_LEFT_OUT_WITHOUT_COMMIT
        )
    # Git records commit times to the second, so a tie is broken by commit ID.
    _, inputs_commit = max(
        (datetime.fromisoformat(last_commits[path].date), last_commits[path].commit)
        for path in paths
    )
    return _Checked(role_files=role_files, inputs_commit=inputs_commit)


def _git_blob_id(content: bytes) -> str:
    """The git blob ID Hugging Face lists as a file's ``oid``."""

    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def _matches_listing(content: bytes, listed: DatasetFile) -> bool:
    if len(content) != listed.size:
        return False
    if listed.lfs_sha256 is not None:
        return hashlib.sha256(content).hexdigest() == listed.lfs_sha256
    return _git_blob_id(content) == listed.blob_id


@dataclass
class _Progress:
    """What an error outcome should still report about its submission."""

    submission_id: str | None = None
    new_submission: bool = False


def _outcome(
    build: PastBuild,
    record: SubmissionRecord,
    *,
    new_submission: bool,
    job_outcome: JobOutcome | None,
) -> ProjectionOutcome:
    reason = None
    if record.rejection is not None:
        reason = f"{record.rejection.get('code')}: {record.rejection.get('message')}"
    if record.status in {"succeeded", "rejected", "failed"}:
        outcome: ProjectionOutcomeKind = record.status
    elif record.status == "processing":
        outcome = "processing"
        reason = _LEASED_ELSEWHERE
    else:
        outcome = "queued"
        reason = (
            _RELEASED_FOR_RETRY if job_outcome is JobOutcome.RETRY else _NOT_CLAIMED
        )
    return ProjectionOutcome(
        run_id=build.run_id,
        outcome=outcome,
        reason=reason,
        submission_id=record.submission_id,
        new_submission=new_submission,
        result=record.result if record.status == "succeeded" else None,
    )


def _accept(
    record: SubmissionRecord,
    stored_files: list[tuple[_SubmittedFile, Path]],
    *,
    submissions: SubmissionStore,
    objects: IntakeObjectWriter,
) -> SubmissionRecord:
    """Create the files as uploads would, then complete the submission."""

    submission_id = record.submission_id
    for submitted, path in stored_files:
        try:
            objects.create_object(
                intake_object_name(submission_id, submitted.name),
                path,
                sha256=submitted.sha256,
            )
        except StoredObjectConflictError:
            # Completion's check below rejects the submission.
            continue
    stored = objects.list_objects(intake_object_prefix(submission_id))
    problems = storage_problems(record, stored)
    rejection = stored_object_mismatch(problems)
    if rejection is not None:
        return submissions.reject_submission(submission_id, rejection)
    if problems:
        raise RuntimeError(
            f"Files of {submission_id} were not stored as created: "
            + ", ".join(problem["name"] for problem in problems)
        )
    return submissions.accept_submission(
        submission_id, stored_generations(record, stored)
    )


def _project(
    build: PastBuild,
    reader: HuggingFaceReader,
    checked: _Checked,
    progress: _Progress,
    *,
    submissions: SubmissionStore,
    objects: IntakeObjectWriter,
    handlers: Mapping[str, JobHandler],
    holder: str,
) -> ProjectionOutcome:
    assert build.dataset is not None and build.revision is not None
    with tempfile.TemporaryDirectory(prefix="past-build-projection-") as directory:
        stored_files = []
        for role, listed in checked.role_files:
            content = reader.read_file(build.dataset, listed.path, build.revision)
            if not _matches_listing(content, listed):
                raise RuntimeError(
                    f"{listed.path} as downloaded differs from Hugging Face's listing."
                )
            name = listed.path.rpartition("/")[2]
            path = Path(directory) / name
            path.write_bytes(content)
            submitted = _SubmittedFile(
                name=name,
                role=role,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            stored_files.append((submitted, path))
        try:
            request = _submission_request(
                build,
                checked.inputs_commit,
                [submitted for submitted, _ in stored_files],
            )
        except ValidationError as error:
            return ProjectionOutcome(
                run_id=build.run_id, outcome="refused", reason=_refusal(error)
            )
        record, new_submission = submissions.create_submission(request, COLLECTOR_OWNER)
        progress.submission_id = record.submission_id
        progress.new_submission = new_submission
        if record.status == "awaiting_upload":
            record = _accept(
                record, stored_files, submissions=submissions, objects=objects
            )
    job_outcome = None
    if record.status in {"queued", "processing"} and record.job_id is not None:
        job_outcome = run_job(
            record.job_id,
            submissions=submissions,
            objects=objects,
            handlers=handlers,
            holder=holder,
        )
        refreshed = submissions.get_submission(record.submission_id)
        assert refreshed is not None
        record = refreshed
    return _outcome(
        build, record, new_submission=new_submission, job_outcome=job_outcome
    )


def _error(
    build: PastBuild, error: Exception, progress: _Progress | None = None
) -> ProjectionOutcome:
    return ProjectionOutcome(
        run_id=build.run_id,
        outcome="error",
        reason=f"{type(error).__name__}: {error}",
        submission_id=None if progress is None else progress.submission_id,
        new_submission=False if progress is None else progress.new_submission,
        incomplete=True,
    )


@dataclass
class _DatasetListings:
    """Each dataset's file list by folder, read once when first needed."""

    reader: HuggingFaceReader
    listings: dict[str, dict[str, dict[str, DatasetFile]] | Exception] = field(
        default_factory=dict
    )

    def files_by_folder(
        self, dataset: str, revision: str
    ) -> dict[str, dict[str, DatasetFile]]:
        if dataset not in self.listings:
            try:
                files_by_folder: dict[str, dict[str, DatasetFile]] = {}
                for dataset_file in self.reader.list_files(dataset, revision):
                    folder, _, name = dataset_file.path.rpartition("/")
                    files_by_folder.setdefault(folder, {})[name] = dataset_file
                self.listings[dataset] = files_by_folder
            except Exception as error:
                self.listings[dataset] = error
        listing = self.listings[dataset]
        if isinstance(listing, Exception):
            raise listing
        return listing


def reproject_past_builds(
    inventory: PastBuildInventory,
    reader: HuggingFaceReader,
    *,
    submissions: SubmissionStore,
    objects: IntakeObjectWriter | None,
    handlers: Mapping[str, JobHandler],
    holder: str,
    dry_run: bool = False,
) -> Iterator[ProjectionOutcome]:
    """Project every eligible past build, yielding each outcome in run-ID order.

    A dry run makes the same checks from Hugging Face's listings, and
    downloads no graph inputs and writes nothing.
    """

    if not dry_run and objects is None:
        raise ValueError("Projecting past builds needs the intake bucket.")
    listings = _DatasetListings(reader)
    for build in inventory.builds:
        reason = left_out_reason(build)
        if reason is not None:
            yield ProjectionOutcome(
                run_id=build.run_id,
                outcome="left_out",
                reason=reason,
                incomplete=build.commit_class == "unknown" and build.class_retry_later,
            )
            continue
        assert build.dataset is not None and build.revision is not None
        progress = _Progress()
        try:
            checked = _check(
                build, reader, listings.files_by_folder(build.dataset, build.revision)
            )
            if isinstance(checked, ProjectionOutcome):
                yield checked
            elif dry_run:
                yield ProjectionOutcome(run_id=build.run_id, outcome="would_reproject")
            else:
                assert objects is not None
                yield _project(
                    build,
                    reader,
                    checked,
                    progress,
                    submissions=submissions,
                    objects=objects,
                    handlers=handlers,
                    holder=holder,
                )
        except Exception as error:
            yield _error(build, error, progress)
