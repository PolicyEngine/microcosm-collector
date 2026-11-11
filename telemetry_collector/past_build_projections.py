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
  folder at its last commit. The submission ID is the hash of the request
  (``derive_submission_id``), so another run makes the same submission.
- **Licensed data:** true unless the build was staged in a public dataset
  (``HUGGINGFACE_DATASETS``), so a private dataset's documents are private.

Each file is created create-only in the intake bucket under the submission's
ID, as an upload would be, and the submission is accepted as completion
accepts one (``storage_problems``). Its job then runs at once through the
worker's ``run_job`` and the registered handler, so the document is checked,
validated, published and recorded as a new build's is. A job released for a
retry stays queued, and the scheduled recovery dispatches it to the worker job.

Builds that can't be projected are listed as ``left_out`` with the reason. A
dry run lists what would be projected, and reads and writes nothing.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import ValidationError

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.google_cloud import StoredObjectConflictError
from telemetry_collector.huggingface_read import HUGGINGFACE_DATASETS, HuggingFaceReader
from telemetry_collector.intake import IntakeObjectStore
from telemetry_collector.intake_api import storage_problems, stored_object_mismatch
from telemetry_collector.past_builds import (
    EXECUTION_OVERLAY,
    GRAPH_SCHEMA,
    STAGED_PREFIX,
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
from telemetry_collector.worker import JobHandler, run_job

ProjectionOutcomeKind = Literal[
    "left_out",
    "would_reproject",
    "refused",
    "queued",
    "succeeded",
    "rejected",
    "failed",
]

#: The owner of the submissions the collector makes for past builds. No
#: Hugging Face user has this ID, so no build can complete or read them.
COLLECTOR_OWNER: Final = HuggingFacePrincipal(
    user_id="microcosm-collector", username="microcosm-collector", organizations=()
)
#: Microcosm registers UK runs as GB; the release datasets call the country uk.
_SUBMISSION_COUNTRY_BY_RELEASE_COUNTRY: Final = {"uk": "GB"}
_PUBLIC_DATASETS: Final = frozenset(
    dataset.name for dataset in HUGGINGFACE_DATASETS if dataset.public
)
_UPSTREAM_FILE_NAME: Final = re.compile(UPSTREAM_FILE_NAME_PATTERN)
_LEFT_OUT_WITHOUT_OVERLAY: Final = (
    "its staged folder holds the schema without the execution overlay"
)
_LEFT_OUT_WITHOUT_INPUTS: Final = "it has nothing on Hugging Face to project"
_RETRIED_BY_RECOVERY: Final = (
    "its job was released for a retry; the scheduled recovery dispatches it to "
    "the worker job"
)


class IntakeObjectWriter(IntakeObjectStore, Protocol):
    """The intake bucket, which the command also creates objects in."""

    def create_object(self, object_name: str, source: Path, *, sha256: str) -> bool:
        """Create the object once; False when an equal one is kept."""


@dataclass(frozen=True)
class ProjectionOutcome:
    """What happened to one past build."""

    run_id: str
    #: ``left_out``, ``would_reproject`` or ``refused``, or else the
    #: submission's status: ``succeeded``, ``rejected``, ``failed``, or
    #: ``queued`` while its job waits for a retry.
    outcome: ProjectionOutcomeKind
    reason: str | None = None
    submission_id: str | None = None
    #: Whether this run created the submission; false when it already existed.
    new_submission: bool = False
    #: The handler's result, for a submission that succeeded.
    result: dict[str, Any] | None = None


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
class _StagedInput:
    name: str
    role: str
    path: Path
    size: int
    sha256: str


def _submission_request(
    build: PastBuild, folder_commit: str, inputs: list[_StagedInput]
) -> SubmissionRequest:
    assert build.dataset is not None and build.staged_folder is not None
    country_code = _SUBMISSION_COUNTRY_BY_RELEASE_COUNTRY.get(
        build.country_code, build.country_code.upper()
    )
    return SubmissionRequest.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "kind": "orrery-inputs",
            "licensed_data": build.dataset not in _PUBLIC_DATASETS,
            "provenance": {
                "run_id": build.run_id,
                "build_id": build.run_id,
                "country_code": country_code,
                "release": None,
                "huggingface": {
                    "repo": build.dataset,
                    "commit": folder_commit,
                    "folder": build.staged_folder,
                },
            },
            "files": [
                {
                    "name": staged.name,
                    "role": staged.role,
                    "size": staged.size,
                    "sha256": staged.sha256,
                }
                for staged in inputs
            ],
        }
    )


def _outcome(
    build: PastBuild, record: SubmissionRecord, *, new_submission: bool
) -> ProjectionOutcome:
    reason = None
    if record.rejection is not None:
        reason = f"{record.rejection.get('code')}: {record.rejection.get('message')}"
    if record.status in {"succeeded", "rejected", "failed"}:
        outcome: ProjectionOutcomeKind = record.status
    else:
        outcome = "queued"
        reason = _RETRIED_BY_RECOVERY
    return ProjectionOutcome(
        run_id=build.run_id,
        outcome=outcome,
        reason=reason,
        submission_id=record.submission_id,
        new_submission=new_submission,
        result=record.result if record.status == "succeeded" else None,
    )


def _project(
    build: PastBuild,
    reader: HuggingFaceReader,
    *,
    folder_commit: str,
    upstream_names: list[str],
    submissions: SubmissionStore,
    objects: IntakeObjectWriter,
    handlers: Mapping[str, JobHandler],
    holder: str,
) -> ProjectionOutcome:
    assert build.dataset is not None and build.revision is not None
    roles = [(GRAPH_SCHEMA, "schema"), (EXECUTION_OVERLAY, "execution_overlay")]
    roles += [(name, "upstream") for name in upstream_names]
    with tempfile.TemporaryDirectory(prefix="past-build-projection-") as directory:
        inputs = []
        for name, role in roles:
            content = reader.read_file(
                build.dataset, f"{build.staged_folder}/{name}", build.revision
            )
            path = Path(directory) / name
            path.write_bytes(content)
            inputs.append(
                _StagedInput(
                    name=name,
                    role=role,
                    path=path,
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        try:
            request = _submission_request(build, folder_commit, inputs)
        except ValidationError as error:
            return ProjectionOutcome(
                run_id=build.run_id,
                outcome="refused",
                reason="; ".join(detail["msg"] for detail in error.errors()),
            )
        record, new_submission = submissions.create_submission(request, COLLECTOR_OWNER)
        submission_id = record.submission_id
        if record.status == "awaiting_upload":
            for staged in inputs:
                try:
                    objects.create_object(
                        intake_object_name(submission_id, staged.name),
                        staged.path,
                        sha256=staged.sha256,
                    )
                except StoredObjectConflictError:
                    # Completion's check below rejects the submission.
                    continue
            stored = objects.list_objects(intake_object_prefix(submission_id))
            problems = storage_problems(record, stored)
            rejection = stored_object_mismatch(problems)
            if rejection is not None:
                record = submissions.reject_submission(submission_id, rejection)
            elif problems:
                raise RuntimeError(
                    f"Files of {submission_id} were not stored as created: "
                    + ", ".join(problem["name"] for problem in problems)
                )
            else:
                record = submissions.accept_submission(
                    submission_id,
                    {
                        file["name"]: stored[
                            intake_object_name(submission_id, file["name"])
                        ].generation
                        for file in record.files
                    },
                )
    if record.status == "queued" and record.job_id is not None:
        run_job(
            record.job_id,
            submissions=submissions,
            objects=objects,
            handlers=handlers,
            holder=holder,
        )
        refreshed = submissions.get_submission(submission_id)
        assert refreshed is not None
        record = refreshed
    return _outcome(build, record, new_submission=new_submission)


def reproject_past_builds(
    inventory: PastBuildInventory,
    reader: HuggingFaceReader,
    *,
    submissions: SubmissionStore,
    objects: IntakeObjectWriter | None,
    handlers: Mapping[str, JobHandler],
    holder: str,
    dry_run: bool = False,
) -> list[ProjectionOutcome]:
    """Project every eligible past build, in the inventory's run-ID order."""

    eligible = [build for build in inventory.builds if left_out_reason(build) is None]
    if dry_run:
        return [
            ProjectionOutcome(run_id=build.run_id, outcome="would_reproject")
            if left_out_reason(build) is None
            else ProjectionOutcome(
                run_id=build.run_id, outcome="left_out", reason=left_out_reason(build)
            )
            for build in inventory.builds
        ]
    if objects is None:
        raise ValueError("Projecting past builds needs the intake bucket.")
    folder_commits: dict[str, dict[str, str]] = {}
    upstream_names: dict[str, dict[str, list[str]]] = {}
    for dataset, revision in sorted(
        {(build.dataset, build.revision) for build in eligible if build.dataset}
    ):
        assert revision is not None
        folder_commits[dataset] = reader.folder_commits(
            dataset, STAGED_PREFIX, revision
        )
        names_by_folder: dict[str, list[str]] = {}
        for dataset_file in reader.list_files(dataset, revision):
            folder, _, name = dataset_file.path.rpartition("/")
            if _UPSTREAM_FILE_NAME.fullmatch(name):
                names_by_folder.setdefault(folder, []).append(name)
        upstream_names[dataset] = {
            folder: sorted(names) for folder, names in names_by_folder.items()
        }

    outcomes = []
    for build in inventory.builds:
        reason = left_out_reason(build)
        if reason is not None:
            outcomes.append(
                ProjectionOutcome(
                    run_id=build.run_id, outcome="left_out", reason=reason
                )
            )
            continue
        assert build.dataset is not None and build.staged_folder is not None
        folder_commit = folder_commits[build.dataset].get(build.staged_folder)
        if folder_commit is None:
            outcomes.append(
                ProjectionOutcome(
                    run_id=build.run_id,
                    outcome="left_out",
                    reason="Hugging Face lists no last commit for its staged folder",
                )
            )
            continue
        outcomes.append(
            _project(
                build,
                reader,
                folder_commit=folder_commit,
                upstream_names=upstream_names[build.dataset].get(
                    build.staged_folder, []
                ),
                submissions=submissions,
                objects=objects,
                handlers=handlers,
                holder=holder,
            )
        )
    return outcomes
