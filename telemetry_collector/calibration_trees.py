"""Build calibration trees from ``calibration-diagnostics`` submissions.

The handler plans a tree from the submission's provenance: a build folder
``releases/<release>/<build>`` gives a release tree whose release ID is
``<release>/<build>``, and a staged bundle ``staged/<run_id>`` gives a staging
tree of that run. It downloads the files into a new directory for each
attempt and runs the tree package's Bun worker on them with the Blob token
left out of its environment, so the token never reaches the builder's
arguments, environment or output. (This is not isolation: the builder runs as
the same user as the worker, which holds the token in its own environment.)
It then runs the package's publication command, which holds the token, inside
one Postgres transaction lock, so collector writers add to the manifest one
at a time.

Nothing here reads Hugging Face: every value comes from the submission. The
publication time recorded in the tree is the submission's ``accepted_at``, so
reprocessing a submission builds the same tree. A release build becomes its
country's latest when its release is the country's default release in
``DEFAULT_RELEASES``.

The worker registers this handler only where ``COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN``
is set (``handlers_from_environment``); elsewhere submissions stay in intake
storage and can be reprocessed once it is.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa

from telemetry_collector.submission_store import SubmissionRecord
from telemetry_collector.submissions import INTAKE_ONLY_HANDLER
from telemetry_collector.worker import (
    DEFAULT_HANDLERS,
    WORKER_TASK_TIMEOUT_SECONDS,
    JobContext,
    JobHandler,
    SubmissionRejectedError,
)

PACKAGE_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "packages" / "calibration-tree"
)
WORKER_SCRIPT = PACKAGE_DIRECTORY / "src" / "calibration-tree-worker.ts"
PUBLICATION_SCRIPT = PACKAGE_DIRECTORY / "src" / "calibration-tree-publication.ts"

#: The worker job's setting: the read-write token of this environment's
#: calibration tree Blob store, a Secret Manager secret.
BLOB_TOKEN_SETTING = "COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN"
#: The name the publication command reads the token from.
PUBLICATION_TOKEN_VARIABLE = "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN"
#: The tree package's part size limits, passed on to its worker when set.
SIZE_LIMIT_SETTINGS = (
    "CALIBRATION_TREE_MAX_RAW_BYTES",
    "CALIBRATION_TREE_MAX_GZIP_BYTES",
)

#: Each country's release repo, and the repos its staged bundles may come from.
RELEASE_REPOS: Mapping[str, str] = {
    "us": "policyengine/populace-us",
    "uk": "policyengine/populace-uk-private",
    "be": "policyengine/populace-be-private",
}
STAGING_REPOS: Mapping[str, str] = {
    "us": "policyengine/populace-us-staging",
    "uk": "policyengine/populace-uk-staging",
}
#: Each country's default (national) release. A build of it becomes the
#: country's latest. The US's is added once its stable releases are agreed.
DEFAULT_RELEASES: Mapping[str, str] = {"uk": "microcosm-uk-2024-25-national"}

#: The key of the transaction lock every manifest writer takes.
MANIFEST_LOCK_KEY = 4_771_046_117_206_302_722
# Most of the task's hour goes to building; the rest to publishing.
BUILD_TIMEOUT_SECONDS = WORKER_TASK_TIMEOUT_SECONDS * 3 // 4
PUBLICATION_TIMEOUT_SECONDS = WORKER_TASK_TIMEOUT_SECONDS // 6
MAX_MESSAGE_CHARACTERS = 500

EXIT_UNBUILDABLE_INPUT = 2
EXIT_ENTRY_REFUSED = 2

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_RELEASE_FOLDER = re.compile(rf"releases/(?P<release>{_SEGMENT})/(?P<build>{_SEGMENT})")
_STAGED_FOLDER = re.compile(rf"staged/(?P<run>{_SEGMENT})")
# The submission's roles, as the worker's request names them.
_ARTIFACTS = {
    "calibration_diagnostics": "calibrationDiagnostics",
    "build_manifest": "buildManifest",
    "release_manifest": "releaseManifest",
    "demographics": "demographics",
}


@dataclass(frozen=True)
class TreeBuildPlan:
    """The tree a submission builds, from its provenance alone."""

    kind: Literal["release", "staging"]
    country: str
    folder: str
    hf_repo: str
    hf_commit: str
    created_at: str
    run_id: str
    release_id: str | None
    label: str | None
    make_latest: bool


@dataclass(frozen=True)
class KeptInIntakeStorage:
    """A submission no tree is built from yet; reprocessing can build it later."""

    reason: Literal["folder_not_recognised", "country_not_registered"]


def _accepted_at(submission: SubmissionRecord) -> str:
    if submission.accepted_at is None:
        raise RuntimeError("A submission being processed has no accepted_at.")
    return (
        submission.accepted_at.astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def plan_tree_build(
    submission: SubmissionRecord,
) -> TreeBuildPlan | KeptInIntakeStorage:
    """Plan the tree ``submission`` builds, or say why it builds none yet.

    Raises ``SubmissionRejectedError`` when the provenance contradicts its own
    folder or names a repo the country does not publish from.
    """

    provenance = submission.provenance
    huggingface = provenance["huggingface"]
    folder = huggingface["folder"]
    country = provenance["country_code"].lower()
    release_folder = _RELEASE_FOLDER.fullmatch(folder)
    staged_folder = _STAGED_FOLDER.fullmatch(folder)
    if release_folder is None and staged_folder is None:
        return KeptInIntakeStorage("folder_not_recognised")
    if country not in RELEASE_REPOS:
        return KeptInIntakeStorage("country_not_registered")
    allowed_repos = {RELEASE_REPOS[country]}
    if staged_folder is not None and country in STAGING_REPOS:
        allowed_repos.add(STAGING_REPOS[country])
    if huggingface["repo"] not in allowed_repos:
        raise SubmissionRejectedError(
            "repo_not_registered",
            f"{huggingface['repo']} is not a repo {country.upper()} builds are published from.",
        )
    shared = {
        "country": country,
        "folder": folder,
        "hf_repo": huggingface["repo"],
        "hf_commit": huggingface["commit"],
        "created_at": _accepted_at(submission),
        "run_id": provenance["run_id"],
    }
    if release_folder is not None:
        release, build = release_folder["release"], release_folder["build"]
        if provenance["release"] != release or provenance["build_id"] != build:
            raise SubmissionRejectedError(
                "provenance_mismatch",
                f"{folder} is not the folder of release {provenance['release']} "
                f"build {provenance['build_id']}.",
            )
        return TreeBuildPlan(
            kind="release",
            release_id=f"{release}/{build}",
            label=None,
            make_latest=DEFAULT_RELEASES.get(country) == release,
            **shared,
        )
    if staged_folder["run"] != provenance["run_id"]:
        raise SubmissionRejectedError(
            "provenance_mismatch",
            f"{folder} is not the staged bundle of run {provenance['run_id']}.",
        )
    return TreeBuildPlan(
        kind="staging",
        release_id=None,
        # The candidate release the run was staged for; the publisher also
        # labels a run that names none with its run ID.
        label=provenance["release"] or provenance["run_id"],
        make_latest=False,
        **shared,
    )


def worker_request(
    plan: TreeBuildPlan, files: Mapping[str, tuple[str, Path]]
) -> dict[str, Any]:
    """The Bun worker's request: ``files`` maps each role to its name and local copy."""

    artifacts: dict[str, Any] = dict.fromkeys(_ARTIFACTS.values())
    for role, (name, local_path) in files.items():
        artifacts[_ARTIFACTS[role]] = {
            "path": f"{plan.folder}/{name}",
            "file": str(local_path),
        }
    shared = {
        "country": plan.country,
        "hfRepo": plan.hf_repo,
        "hfCommitSha": plan.hf_commit,
        "createdAt": plan.created_at,
        "artifacts": artifacts,
    }
    if plan.kind == "staging":
        return {"kind": "staging", "runId": plan.run_id, "label": plan.label, **shared}
    return {
        "kind": "release",
        "releaseId": plan.release_id,
        "releaseDirectory": plan.release_id,
        **shared,
    }


CommandRunner = Callable[
    [Sequence[str], Mapping[str, str], float], subprocess.CompletedProcess[str]
]
ManifestLock = Callable[[], AbstractContextManager[None]]


def run_bun(
    argv: Sequence[str], env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run one Bun process with exactly ``env``; a timeout fails the attempt."""

    return subprocess.run(
        list(argv),
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
        timeout=timeout,
    )


def postgres_manifest_lock(engine: sa.Engine) -> ManifestLock:
    """A lock held by one transaction at a time across every collector writer."""

    @contextmanager
    def lock() -> Iterator[None]:
        with engine.begin() as connection:
            connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": MANIFEST_LOCK_KEY},
            )
            yield

    return lock


def _shortened(text: str) -> str:
    text = text.strip() or "(no message)"
    return (
        text
        if len(text) <= MAX_MESSAGE_CHARACTERS
        else text[: MAX_MESSAGE_CHARACTERS - 1] + "…"
    )


class CalibrationTreeCommandError(RuntimeError):
    """A Bun process failed in a way another attempt may get past."""


def _answer(completed: subprocess.CompletedProcess[str], step: str) -> dict[str, Any]:
    lines = completed.stdout.strip().splitlines()
    try:
        answer = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as error:
        raise CalibrationTreeCommandError(
            f"The tree {step} printed no JSON answer."
        ) from error
    if not isinstance(answer, dict):
        raise CalibrationTreeCommandError(f"The tree {step} printed no JSON answer.")
    return answer


def calibration_tree_handler(
    *,
    blob_token: str,
    manifest_lock: ManifestLock,
    bun: str = "bun",
    size_limits: Mapping[str, str] | None = None,
    run_command: CommandRunner = run_bun,
) -> JobHandler:
    """The ``calibration-diagnostics`` handler, writing with ``blob_token``."""

    base_environment = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
    build_environment = {**base_environment, **(size_limits or {})}
    publication_environment = {
        **base_environment,
        PUBLICATION_TOKEN_VARIABLE: blob_token,
    }

    def build_tree(context: JobContext) -> dict[str, Any]:
        plan = plan_tree_build(context.submission)
        if isinstance(plan, KeptInIntakeStorage):
            return {
                "processed_by": INTAKE_ONLY_HANDLER,
                "verified_files": len(context.files()),
                "reason": plan.reason,
            }
        with tempfile.TemporaryDirectory(prefix="calibration-tree-") as directory:
            attempt_directory = Path(directory)
            files_directory = attempt_directory / "files"
            files_directory.mkdir()
            files = {
                file["role"]: (
                    file["name"],
                    context.download(file["name"], files_directory / file["name"]),
                )
                for file in context.files()
            }
            request_path = attempt_directory / "request.json"
            request_path.write_text(
                json.dumps(worker_request(plan, files)), encoding="utf-8"
            )
            bundle_directory = attempt_directory / "trees"
            built = run_command(
                [
                    bun,
                    "run",
                    str(WORKER_SCRIPT),
                    str(request_path),
                    str(bundle_directory),
                ],
                build_environment,
                BUILD_TIMEOUT_SECONDS,
            )
            if built.returncode == EXIT_UNBUILDABLE_INPUT:
                raise SubmissionRejectedError(
                    "calibration_tree_unbuildable", _shortened(built.stderr)
                )
            if built.returncode != 0:
                raise CalibrationTreeCommandError(
                    f"The tree worker exited with {built.returncode}: {_shortened(built.stderr)}"
                )
            publication_path = attempt_directory / "publication.json"
            publication_path.write_text(
                json.dumps(
                    {
                        "bundleDirectory": str(bundle_directory),
                        "worker": _answer(built, "worker"),
                        "makeLatest": plan.make_latest,
                    }
                ),
                encoding="utf-8",
            )
            with manifest_lock():
                published = run_command(
                    [bun, "run", str(PUBLICATION_SCRIPT), str(publication_path)],
                    publication_environment,
                    PUBLICATION_TIMEOUT_SECONDS,
                )
            if published.returncode == EXIT_ENTRY_REFUSED:
                raise SubmissionRejectedError(
                    "calibration_tree_refused", _shortened(published.stderr)
                )
            if published.returncode != 0:
                raise CalibrationTreeCommandError(
                    f"The tree publication exited with {published.returncode}: "
                    f"{_shortened(published.stderr)}"
                )
            answer = _answer(published, "publication")
        return {
            "processed_by": "calibration-tree",
            "build_artifact_id": answer["buildArtifactId"],
            "kind": answer["kind"],
            "uploaded_parts": answer["uploadedParts"],
            "kept_parts": answer["keptParts"],
            "made_latest": answer["madeLatest"],
        }

    return build_tree


def handlers_from_environment(
    *, engine: sa.Engine | None, environ: Mapping[str, str] = os.environ
) -> Mapping[str, JobHandler]:
    """The worker's handlers: the tree handler only where its Blob token is set."""

    blob_token = environ.get(BLOB_TOKEN_SETTING, "").strip()
    if not blob_token:
        return DEFAULT_HANDLERS
    size_limits = {
        name: environ[name].strip()
        for name in SIZE_LIMIT_SETTINGS
        if environ.get(name, "").strip()
    }

    def manifest_lock() -> AbstractContextManager[None]:
        if engine is None:
            raise RuntimeError("The manifest lock needs the collector's database.")
        return postgres_manifest_lock(engine)()

    return {
        **DEFAULT_HANDLERS,
        "calibration-diagnostics": calibration_tree_handler(
            blob_token=blob_token, manifest_lock=manifest_lock, size_limits=size_limits
        ),
    }
