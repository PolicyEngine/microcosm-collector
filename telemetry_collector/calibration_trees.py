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
at a time. ``build_and_publish`` is that build and publication, shared with
the backfill (``telemetry_collector.calibration_tree_backfill``).

The tree's files and provenance come from the submission. The publication
time recorded in the tree is the submission's ``accepted_at``, so
reprocessing a submission builds the same tree. Only the latest build is
decided from Hugging Face: a release build becomes its country's latest when
the release repo's ``releases.json``, read at the repo's head commit when the
tree is built, names it as its default release's current build, as the
GitHub Actions publisher decides. A repo without ``releases.json`` keeps the
rule of ``DEFAULT_RELEASES``: a build of the country's default release there
becomes its latest. A failed read of ``releases.json`` fails the attempt
before anything is downloaded, so it runs again; a ``releases.json`` that
breaks the record's rules publishes the tree without moving the latest, and
the result says why (``latest_skipped``). Staging trees never become the
latest.

The worker registers this handler only where ``COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN``
is set (``registered_handlers``, which ``telemetry_collector.handlers`` reads),
and then needs ``COLLECTOR_HF_READ_TOKEN`` too; elsewhere submissions stay in
intake storage and can be reprocessed once it is.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC
from pathlib import Path
from typing import Any, Final, Literal

import sqlalchemy as sa

from telemetry_collector.huggingface_read import (
    HuggingFaceReader,
    HuggingFaceReadError,
    HuggingFaceReadSettings,
    is_missing_path,
)
from telemetry_collector.submission_store import SubmissionRecord
from telemetry_collector.submissions import INTAKE_ONLY_HANDLER
from telemetry_collector.worker import (
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
#: Prints the store's manifest; it only reads, with the publication's token.
MANIFEST_READ_SCRIPT = PACKAGE_DIRECTORY / "src" / "calibration-tree-manifest-read.ts"

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
#: Each country's default (national) release, for a release repo without
#: ``releases.json``: there, a build of it becomes the country's latest. The
#: US's is added once its stable releases are agreed.
DEFAULT_RELEASES: Mapping[str, str] = {"uk": "microcosm-uk-2024-25-national"}

#: The key of the transaction lock every manifest writer takes.
MANIFEST_LOCK_KEY = 4_771_046_117_206_302_722
# Most of the task's hour goes to building; the rest to publishing.
BUILD_TIMEOUT_SECONDS = WORKER_TASK_TIMEOUT_SECONDS * 3 // 4
PUBLICATION_TIMEOUT_SECONDS = WORKER_TASK_TIMEOUT_SECONDS // 6
MANIFEST_READ_TIMEOUT_SECONDS = 5 * 60
MAX_MESSAGE_CHARACTERS = 500

EXIT_UNBUILDABLE_INPUT = 2
EXIT_ENTRY_REFUSED = 2

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
#: A release, build or run name: one path segment that cannot leave its folder.
SAFE_IDENTIFIER: Final = re.compile(_SEGMENT)
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
    """The tree a submission, or a backfilled build, builds."""

    kind: Literal["release", "staging"]
    country: str
    folder: str
    hf_repo: str
    hf_commit: str
    #: The publication time the tree records, or ``None`` for an undated build.
    created_at: str | None
    #: The staged run; for a release, the submission's run, if any.
    run_id: str | None
    release_id: str | None
    label: str | None
    make_latest: bool
    #: Each role's file, by its full path in the Hugging Face dataset.
    paths: Mapping[str, str] = field(default_factory=dict)
    #: The ``releases/`` folder a release's files are in; ``release_id`` when
    #: ``None``.
    release_directory: str | None = None


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
    # The tree worker never takes "latest" as a run ID.
    if staged_folder is not None and staged_folder["run"] == "latest":
        staged_folder = None
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
        "paths": {
            file["role"]: f"{folder}/{file['name']}" for file in submission.files
        },
    }
    if release_folder is not None:
        release, build = release_folder["release"], release_folder["build"]
        if provenance["release"] != release or provenance["build_id"] != build:
            raise SubmissionRejectedError(
                "provenance_mismatch",
                f"{folder} is not the folder of release {provenance['release']} "
                f"build {provenance['build_id']}.",
            )
        # DEFAULT_RELEASES's rule; the handler replaces it with releases.json's
        # where the repo has one (release_latest).
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


def worker_request(plan: TreeBuildPlan, files: Mapping[str, Path]) -> dict[str, Any]:
    """The Bun worker's request: ``files`` maps each role to its local copy.

    Each file is named by its path in the dataset, from ``plan.paths``.
    """

    artifacts: dict[str, Any] = dict.fromkeys(_ARTIFACTS.values())
    for role, local_path in files.items():
        artifacts[_ARTIFACTS[role]] = {
            "path": plan.paths[role],
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
        "releaseDirectory": plan.release_directory or plan.release_id,
        **shared,
    }


# releases.json ----------------------------------------------------------------

#: The record of a release repo's builds, read by the latest rule.
RELEASES_RECORD_PATH: Final = "releases.json"
#: The largest releases.json the record's writer accepts.
MAX_RELEASES_RECORD_BYTES: Final = 4 * 1024 * 1024
_ROLE_IDENTIFIER: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RECORD_KEYS: Final = frozenset({"default", "releases", "schema_version"})
_RELEASE_KEYS: Final = frozenset({"builds", "current", "role"})
_BUILD_KEYS: Final = frozenset({"published_at", "withdrawn"})


class ReleasesRecordRefusedError(ValueError):
    """A releases.json that breaks the record's rules."""


def _refuse_record(message: str) -> ReleasesRecordRefusedError:
    return ReleasesRecordRefusedError(f"{RELEASES_RECORD_PATH}: {message}")


def _is_safe_identifier(value: object) -> bool:
    return isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value) is not None


def _reject_constant(constant: str) -> None:
    # JSON.parse refuses NaN and Infinity, which Python's json reads.
    raise ValueError(f"{constant} is not JSON.")


def current_default_build(content: bytes) -> str | None:
    """The current build of the record's default release, as ``<release>/<build>``.

    ``None`` while the record names no default release or that release has no
    current build. A record the tree package's ``parseReleasesRecordBytes``
    would refuse raises ``ReleasesRecordRefusedError``: one larger than 4 MiB,
    not a JSON document, without exactly its keys or ``schema_version`` 1,
    with a release or build name that is not a safe identifier, a role that
    is not one, a malformed build, a ``current`` that is not a listed build
    or is withdrawn, or a ``default`` that is not a listed release.
    """

    if len(content) > MAX_RELEASES_RECORD_BYTES:
        raise _refuse_record(f"larger than {MAX_RELEASES_RECORD_BYTES} bytes.")
    try:
        # As TextDecoder reads it: invalid UTF-8 refused, a leading BOM dropped.
        document = json.loads(
            content.decode("utf-8-sig"), parse_constant=_reject_constant
        )
    except ValueError:
        raise _refuse_record("not a readable JSON document.") from None
    if not isinstance(document, dict) or set(document) != _RECORD_KEYS:
        raise _refuse_record("must have exactly default, releases, schema_version.")
    schema_version = document["schema_version"]
    if isinstance(schema_version, bool) or schema_version != 1:
        raise _refuse_record("schema_version must be 1.")
    releases = document["releases"]
    if not isinstance(releases, dict):
        raise _refuse_record("releases must be an object.")
    for release, entry in releases.items():
        if not _is_safe_identifier(release):
            raise _refuse_record("a release name is not a safe identifier.")
        if not isinstance(entry, dict) or set(entry) != _RELEASE_KEYS:
            raise _refuse_record(f"release {release} must have builds, current, role.")
        role = entry["role"]
        if not isinstance(role, str) or _ROLE_IDENTIFIER.fullmatch(role) is None:
            raise _refuse_record(f"release {release} has an invalid role.")
        if entry["current"] is not None and not isinstance(entry["current"], str):
            raise _refuse_record(f"release {release} current must be a build or null.")
        builds = entry["builds"]
        if not isinstance(builds, dict):
            raise _refuse_record(f"release {release} builds must be an object.")
        for build, build_entry in builds.items():
            if not _is_safe_identifier(build):
                raise _refuse_record("a build name is not a safe identifier.")
            if (
                not isinstance(build_entry, dict)
                or set(build_entry) != _BUILD_KEYS
                or not isinstance(build_entry["published_at"], str)
                or not build_entry["published_at"]
                or not isinstance(build_entry["withdrawn"], bool)
            ):
                raise _refuse_record(f"build {build} of {release} is malformed.")
    default = document["default"]
    if default is not None and not isinstance(default, str):
        raise _refuse_record("default must be a release name or null.")
    if default is not None and default not in releases:
        raise _refuse_record(f"default {default} is not listed.")
    for release, entry in releases.items():
        current = entry["current"]
        if current is None:
            continue
        if current not in entry["builds"]:
            raise _refuse_record(f"current build {current} of {release} is not listed.")
        if entry["builds"][current]["withdrawn"]:
            raise _refuse_record(f"current build {current} of {release} is withdrawn.")
    if default is None or releases[default]["current"] is None:
        return None
    return f"{default}/{releases[default]['current']}"


@dataclass(frozen=True)
class LatestDecision:
    """Whether a release build's tree becomes its country's latest, and why not."""

    make_latest: bool
    #: Set when releases.json could not decide: ``releases_record_refused``.
    skipped: str | None = None
    skipped_reason: str | None = None


def release_latest(plan: TreeBuildPlan, reader: HuggingFaceReader) -> LatestDecision:
    """Decide a release build's latest from its repo's releases.json at the head.

    The build becomes the latest when the record names it as its default
    release's current build; names are compared, as the GitHub Actions
    publisher compares them first. A 404 without ``RepoNotFound`` or
    ``RevisionNotFound`` means the repo has no record, and ``plan.make_latest``
    (``DEFAULT_RELEASES``'s rule) stands. Any other failed read raises
    ``HuggingFaceReadError``, so the attempt runs again.
    """

    head_commit = reader.head_commit(plan.hf_repo)
    try:
        content = reader.read_file(plan.hf_repo, RELEASES_RECORD_PATH, head_commit)
    except HuggingFaceReadError as error:
        if is_missing_path(error):
            return LatestDecision(make_latest=plan.make_latest)
        raise
    try:
        current = current_default_build(content)
    except ReleasesRecordRefusedError as error:
        return LatestDecision(
            make_latest=False,
            skipped="releases_record_refused",
            skipped_reason=shortened_message(str(error)),
        )
    return LatestDecision(make_latest=current == plan.release_id)


# Building and publishing --------------------------------------------------------

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


def shortened_message(text: str) -> str:
    """``text`` stripped and shortened to ``MAX_MESSAGE_CHARACTERS``.

    An empty message becomes "(no message)". Used for the messages a job's
    result and a backfill outcome record.
    """

    text = text.strip() or "(no message)"
    return (
        text
        if len(text) <= MAX_MESSAGE_CHARACTERS
        else text[: MAX_MESSAGE_CHARACTERS - 1] + "…"
    )


class CalibrationTreeCommandError(RuntimeError):
    """A Bun process failed in a way another attempt may get past."""


class TreeUnbuildableError(Exception):
    """The tree worker refused the files (exit 2); another run cannot help."""


class TreeRefusedError(Exception):
    """The publication command refused the build (exit 2); another run cannot help."""


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


@dataclass(frozen=True)
class TreeCommands:
    """How the tree package's Bun processes run, and the lock around publication.

    The worker runs with ``build_environment``, which never holds the Blob
    token; the publication command and the manifest read run with
    ``publication_environment``, which does.
    """

    bun: str
    run_command: CommandRunner
    manifest_lock: ManifestLock
    build_environment: Mapping[str, str]
    publication_environment: Mapping[str, str] = field(repr=False)


def tree_commands(
    *,
    blob_token: str,
    manifest_lock: ManifestLock,
    bun: str = "bun",
    size_limits: Mapping[str, str] | None = None,
    run_command: CommandRunner = run_bun,
) -> TreeCommands:
    """The Bun processes' settings: only ``PATH``, ``LANG`` and the size limits."""

    base_environment = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
    return TreeCommands(
        bun=bun,
        run_command=run_command,
        manifest_lock=manifest_lock,
        build_environment={**base_environment, **(size_limits or {})},
        publication_environment={
            **base_environment,
            PUBLICATION_TOKEN_VARIABLE: blob_token,
        },
    )


def size_limits_from(environ: Mapping[str, str]) -> dict[str, str]:
    """The tree package's part size limits that ``environ`` sets."""

    return {
        name: environ[name].strip()
        for name in SIZE_LIMIT_SETTINGS
        if environ.get(name, "").strip()
    }


@dataclass(frozen=True)
class PublishedTree:
    """What the publication command reports for a published tree."""

    build_artifact_id: str
    kind: str
    uploaded_parts: int
    kept_parts: int
    made_latest: bool


def build_and_publish(
    plan: TreeBuildPlan,
    files: Mapping[str, Path],
    *,
    directory: Path,
    commands: TreeCommands,
) -> PublishedTree:
    """Build ``plan``'s tree from ``files`` (role to local copy) and publish it.

    The request, the built parts and the publication request are written
    under ``directory``, which must be new for each attempt. The worker runs
    without the Blob token; the publication runs with it, inside the
    manifest lock, which is held for nothing else. Worker exit 2 raises
    ``TreeUnbuildableError`` and publication exit 2 ``TreeRefusedError``;
    any other failure raises ``CalibrationTreeCommandError``, and a timeout
    ``subprocess.TimeoutExpired``.
    """

    request_path = directory / "request.json"
    request_path.write_text(json.dumps(worker_request(plan, files)), encoding="utf-8")
    bundle_directory = directory / "trees"
    built = commands.run_command(
        [
            commands.bun,
            "run",
            str(WORKER_SCRIPT),
            str(request_path),
            str(bundle_directory),
        ],
        commands.build_environment,
        BUILD_TIMEOUT_SECONDS,
    )
    if built.returncode == EXIT_UNBUILDABLE_INPUT:
        raise TreeUnbuildableError(shortened_message(built.stderr))
    if built.returncode != 0:
        raise CalibrationTreeCommandError(
            f"The tree worker exited with {built.returncode}: "
            f"{shortened_message(built.stderr)}"
        )
    publication_path = directory / "publication.json"
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
    with commands.manifest_lock():
        published = commands.run_command(
            [commands.bun, "run", str(PUBLICATION_SCRIPT), str(publication_path)],
            commands.publication_environment,
            PUBLICATION_TIMEOUT_SECONDS,
        )
    if published.returncode == EXIT_ENTRY_REFUSED:
        raise TreeRefusedError(shortened_message(published.stderr))
    if published.returncode != 0:
        raise CalibrationTreeCommandError(
            f"The tree publication exited with {published.returncode}: "
            f"{shortened_message(published.stderr)}"
        )
    answer = _answer(published, "publication")
    return PublishedTree(
        build_artifact_id=answer["buildArtifactId"],
        kind=answer["kind"],
        uploaded_parts=answer["uploadedParts"],
        kept_parts=answer["keptParts"],
        made_latest=answer["madeLatest"],
    )


def read_store_manifest(commands: TreeCommands) -> dict[str, Any]:
    """The store's calibration tree manifest, as the package's manifest read prints it.

    The read takes no lock: it only reads, and the publication command
    checks the manifest again under the lock before it writes.
    """

    completed = commands.run_command(
        [commands.bun, "run", str(MANIFEST_READ_SCRIPT)],
        commands.publication_environment,
        MANIFEST_READ_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise CalibrationTreeCommandError(
            f"The tree manifest read exited with {completed.returncode}: "
            f"{shortened_message(completed.stderr)}"
        )
    return _answer(completed, "manifest read")


# The handler ----------------------------------------------------------------------


def calibration_tree_handler(
    *,
    blob_token: str,
    manifest_lock: ManifestLock,
    reader: HuggingFaceReader,
    bun: str = "bun",
    size_limits: Mapping[str, str] | None = None,
    run_command: CommandRunner = run_bun,
) -> JobHandler:
    """The ``calibration-diagnostics`` handler, writing with ``blob_token``.

    A release build reads its repo's releases.json with ``reader`` to decide
    the latest (``release_latest``).
    """

    commands = tree_commands(
        blob_token=blob_token,
        manifest_lock=manifest_lock,
        bun=bun,
        size_limits=size_limits,
        run_command=run_command,
    )

    def build_tree(context: JobContext) -> dict[str, Any]:
        plan = plan_tree_build(context.submission)
        if isinstance(plan, KeptInIntakeStorage):
            return {
                "processed_by": INTAKE_ONLY_HANDLER,
                "verified_files": len(context.files()),
                "reason": plan.reason,
            }
        latest = LatestDecision(make_latest=False)
        if plan.kind == "release":
            # Before anything is downloaded, so a failed read costs nothing.
            latest = release_latest(plan, reader)
            plan = replace(plan, make_latest=latest.make_latest)
        with tempfile.TemporaryDirectory(prefix="calibration-tree-") as directory:
            attempt_directory = Path(directory)
            files_directory = attempt_directory / "files"
            files_directory.mkdir()
            files = {
                file["role"]: context.download(
                    file["name"], files_directory / file["name"]
                )
                for file in context.files()
            }
            try:
                published = build_and_publish(
                    plan, files, directory=attempt_directory, commands=commands
                )
            except TreeUnbuildableError as error:
                raise SubmissionRejectedError(
                    "calibration_tree_unbuildable", str(error)
                ) from error
            except TreeRefusedError as error:
                raise SubmissionRejectedError(
                    "calibration_tree_refused", str(error)
                ) from error
        result: dict[str, Any] = {
            "processed_by": "calibration-tree",
            "build_artifact_id": published.build_artifact_id,
            "kind": published.kind,
            "uploaded_parts": published.uploaded_parts,
            "kept_parts": published.kept_parts,
            "made_latest": published.made_latest,
        }
        if latest.skipped is not None:
            result["latest_skipped"] = latest.skipped
            result["latest_skipped_reason"] = latest.skipped_reason
        return result

    return build_tree


def registered_handlers(
    *, engine: sa.Engine | None, environ: Mapping[str, str] = os.environ
) -> dict[str, JobHandler]:
    """The tree handler, only where the worker job has its Blob token.

    It also reads releases.json, so it needs ``COLLECTOR_HF_READ_TOKEN``: with
    the Blob token and without the read token, this raises
    ``HuggingFaceReadConfigurationError`` naming the variable.
    """

    blob_token = environ.get(BLOB_TOKEN_SETTING, "").strip()
    if not blob_token:
        return {}
    reader = HuggingFaceReader(HuggingFaceReadSettings.from_environment(environ))

    def manifest_lock() -> AbstractContextManager[None]:
        if engine is None:
            raise RuntimeError("The manifest lock needs the collector's database.")
        return postgres_manifest_lock(engine)()

    return {
        "calibration-diagnostics": calibration_tree_handler(
            blob_token=blob_token,
            manifest_lock=manifest_lock,
            size_limits=size_limits_from(environ),
            reader=reader,
        ),
    }
